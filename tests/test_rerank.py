"""Reranker tests. The real cross-encoder isn't downloaded here; a stub scores by word overlap."""
import os
import re
import sys
import types
from pathlib import Path

import pytest

from ingest.embed import HashEmbedder
from ingest.rerank import get_reranker, rerank


class OverlapReranker:
    """Stand-in cross-encoder: counts question words (5+ letters, crude stem) found in the chunk."""

    def score(self, question, texts):
        words = {w[:6] for w in re.findall(r"[a-z]{5,}", question.lower())}
        return [float(sum(w in t.lower() for w in words)) for t in texts]


def test_rerank_reorders_and_keeps_search_rank():
    rows = [{"content": "billing contact"}, {"content": "about parking"}, {"content": "referred to a collection agency"}]
    out = rerank(OverlapReranker(), "unpaid balances sent to collections?", rows, k=2)
    assert out[0]["content"] == "referred to a collection agency"
    assert out[0]["search_rank"] == 3 and "rerank_score" in out[0]
    assert len(out) == 2


def test_no_reranker_keeps_search_order():
    rows = [{"content": "a"}, {"content": "b"}, {"content": "c"}]
    assert [r["content"] for r in rerank(None, "q", rows, k=2)] == ["a", "b"]


def test_reranker_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("RERANKER", "none")
    assert get_reranker() is None


def test_cross_encoder_wiring(monkeypatch):
    calls = {}

    class TextCrossEncoder:
        def __init__(self, model_name):
            calls["model"] = model_name

        def rerank(self, query, documents):
            calls["query"] = query
            return (float(len(d)) for d in documents)

    mod = types.ModuleType("fastembed.rerank.cross_encoder")
    mod.TextCrossEncoder = TextCrossEncoder
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", mod)
    monkeypatch.delenv("RERANK_MODEL", raising=False)
    monkeypatch.setenv("RERANKER", "local")
    r = get_reranker()
    assert calls["model"] == "Xenova/ms-marco-MiniLM-L-6-v2"
    assert r.score("q", ["a", "abc"]) == [1.0, 3.0]


DB = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not DB, reason="set TEST_DATABASE_URL to run database tests")
def test_retrieve_puts_keyword_only_match_on_top():
    """The exact failure seen on real data: a clause found only by keyword search, buried by fusion."""
    from ingest import store
    from ingest.pipeline import run
    from ingest.retrieve import retrieve

    root = Path(__file__).resolve().parent.parent
    run(root / "data" / "raw", root / "data" / "manifest.json", embedder=HashEmbedder(), database_url=DB)
    conn = store.connect(DB, HashEmbedder().dims)
    rows = retrieve(conn, HashEmbedder(), OverlapReranker(), "when do unpaid balances go to collections?", k=3)
    assert "collection agency" in rows[0]["content"]
    assert rows[0]["search_rank"] > 1          # it was not first before reranking
