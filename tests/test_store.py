"""Database tests. Run only when TEST_DATABASE_URL points at a throwaway Postgres with pgvector."""
import os
from pathlib import Path

import pytest

from ingest.embed import HashEmbedder
from ingest.pipeline import run

DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="set TEST_DATABASE_URL to run database tests")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def conn():
    from ingest import store
    c = store.connect(DB, HashEmbedder().dims)
    c.execute("truncate documents cascade")
    yield c
    c.close()


def ingest(**kw):
    return run(ROOT / "data" / "raw", ROOT / "data" / "manifest.json", embedder=HashEmbedder(),
               database_url=DB, **kw)


def test_ingest_then_skip_then_reuse(conn):
    reports, _ = ingest()
    assert all(r.status == "ingested" for r in reports)
    total = sum(r.chunks for r in reports)
    assert conn.execute("select count(*) n from chunks").fetchone()["n"] == total

    reports, _ = ingest()
    assert all(r.status == "skipped (unchanged)" for r in reports)

    reports, _ = ingest(force=True)
    assert all(r.embedded == 0 and r.reused == r.chunks for r in reports)   # nothing re-embedded
    assert conn.execute("select count(*) n from chunks").fetchone()["n"] == total


def test_search_hides_superseded_by_default(conn):
    from ingest import store
    row = conn.execute("""select c.embed_text from chunks c where c.document_id = '10_price_list_2025_OUTDATED'
                          order by chunk_index limit 1""").fetchone()
    [vec] = HashEmbedder().embed([row["embed_text"]])          # exact match for an outdated chunk
    hidden = store.search(conn, vec, k=3)
    assert all(r["document_id"] != "10_price_list_2025_OUTDATED" for r in hidden)
    shown = store.search(conn, vec, k=3, include_superseded=True)
    assert shown[0]["document_id"] == "10_price_list_2025_OUTDATED"
    assert shown[0]["similarity"] > 0.999


def test_search_can_exclude_restricted(conn):
    from ingest import store
    row = conn.execute("select embed_text from chunks where document_id = '12_home_exercise_handout_low_back' limit 1").fetchone()
    [vec] = HashEmbedder().embed([row["embed_text"]])
    results = store.search(conn, vec, k=5, include_restricted=False)
    assert all(r["document_id"] != "12_home_exercise_handout_low_back" for r in results)


def test_hybrid_search_finds_keyword_matches(conn):
    from ingest import store
    # Hash vectors carry no meaning (random noise on the vector side), so check the keyword ranking itself.
    question = "when do unpaid bills go to collections?"
    rows = store.search(conn, HashEmbedder().embed_query(question), k=store.CANDIDATES * 2, query_text=question)
    hit = next(r for r in rows if "collection agency" in r["content"])
    assert hit["kw_rank"] == 1                                  # rare word "collection" beats common "billing"
    assert hit["document_id"] == "08_financial_agreement_SCANNED"
    kaiser = store.search(conn, HashEmbedder().embed_query("x"), k=60, query_text="do you take kaiser?")
    assert next(r for r in kaiser if r["kw_rank"] == 1)["content"].startswith("Q: Do you accept Kaiser?")


def test_hybrid_search_still_hides_superseded(conn):
    from ingest import store
    question = "self-pay price list 2025 initial evaluation"
    rows = store.search(conn, HashEmbedder().embed_query(question), k=10, query_text=question)
    assert all(r["document_id"] != "10_price_list_2025_OUTDATED" for r in rows)
