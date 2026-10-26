"""Postgres + pgvector storage: schema, idempotent per-document replace, embedding reuse, and search."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .models import Chunk, ParsedDocument

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "001_schema.sql"


def connect(database_url: str, dims: int) -> psycopg.Connection:
    # autocommit: each write group runs in an explicit `with conn.transaction()` block instead.
    # prepare_threshold=None: no server-side prepared statements, so Supabase's transaction pooler works too.
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=True, prepare_threshold=None)
    apply_schema(conn, dims)
    from pgvector.psycopg import register_vector
    register_vector(conn)   # needs the extension to exist, so it runs after the schema
    return conn


def apply_schema(conn: psycopg.Connection, dims: int):
    """Create tables if missing. The vector size comes from the embedder (384 local, 1536 OpenAI)."""
    sql = SCHEMA_FILE.read_text(encoding="utf-8").replace("vector(1536)", f"vector({int(dims)})")
    with conn.transaction():
        conn.execute(sql)


def check_dims(conn: psycopg.Connection, dims: int):
    row = conn.execute("""select atttypmod as dims from pg_attribute
                          where attrelid = 'chunks'::regclass and attname = 'embedding'""").fetchone()
    if row and row["dims"] != dims:
        raise RuntimeError(
            f"The database stores {row['dims']}-dimension vectors but this embedder makes {dims}. "
            f"You switched embedding models: drop the old tables (drop table chunks, documents;) "
            f"and run the ingest again.")


def get_document(conn: psycopg.Connection, doc_id: str) -> dict | None:
    return conn.execute("select file_sha256, pipeline_version from documents where id = %s", (doc_id,)).fetchone()


def cached_embeddings(conn: psycopg.Connection, hashes: list[str], model: str) -> dict:
    """Reuse vectors for chunks whose exact text was already embedded with the same model."""
    if not hashes:
        return {}
    rows = conn.execute("""select distinct on (content_hash) content_hash, embedding from chunks
                           where content_hash = any(%s) and embedding_model = %s""", (hashes, model)).fetchall()
    return {r["content_hash"]: r["embedding"] for r in rows}


def _date(value: str | None):
    return date.fromisoformat(value) if value else None


def replace_document(conn: psycopg.Connection, doc: ParsedDocument, file_sha256: str, pipeline_version: str,
                     chunks: list[Chunk], vectors: list, model: str):
    """Swap a document's rows in one transaction: readers never see a half-ingested document."""
    m = doc.meta
    with conn.transaction():
        conn.execute(
            """insert into documents (id, source, title, doc_type, status, effective_date, expires, superseded_by,
                                      restricted, parser, file_sha256, pipeline_version, warnings, chunk_count, ingested_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
               on conflict (id) do update set source=excluded.source, title=excluded.title, doc_type=excluded.doc_type,
                   status=excluded.status, effective_date=excluded.effective_date, expires=excluded.expires,
                   superseded_by=excluded.superseded_by, restricted=excluded.restricted, parser=excluded.parser,
                   file_sha256=excluded.file_sha256, pipeline_version=excluded.pipeline_version,
                   warnings=excluded.warnings, chunk_count=excluded.chunk_count, ingested_at=now()""",
            (doc.doc_id, doc.source, doc.title, m.get("doc_type"), m.get("status", "current"),
             _date(m.get("effective_date")), _date(m.get("expires")), m.get("superseded_by"),
             bool(m.get("restricted", False)), doc.parser, file_sha256, pipeline_version,
             json.dumps(doc.warnings), len(chunks)))
        conn.execute("delete from chunks where document_id = %s", (doc.doc_id,))
        with conn.cursor() as cur:
            cur.executemany(
                """insert into chunks (document_id, chunk_index, content, embed_text, metadata, content_hash,
                                       embedding_model, embedding)
                   values (%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(c.doc_id, c.chunk_index, c.content, c.embed_text, json.dumps(c.metadata), c.content_hash,
                  model, vec) for c, vec in zip(chunks, vectors)])


def prune(conn: psycopg.Connection, keep_ids: set[str]) -> list[str]:
    """Delete documents whose source file is gone from data/raw."""
    rows = conn.execute("select id from documents").fetchall()
    gone = [r["id"] for r in rows if r["id"] not in keep_ids]
    if gone:
        conn.execute("delete from documents where id = any(%s)", (gone,))
    return gone


RRF_K = 60          # standard reciprocal-rank-fusion constant: dampens the gap between rank 1 and rank 2
CANDIDATES = 30     # how deep each search looks before the two lists are fused


def search(conn: psycopg.Connection, query_vector, k: int = 5, query_text: str | None = None,
           include_superseded: bool = False, include_restricted: bool = True) -> list[dict]:
    """Top-k chunks for a question.

    With query_text: hybrid search. A vector search (meaning) and a keyword search (exact words: names,
    codes, "collection") each rank up to CANDIDATES chunks, and the two rankings are merged with
    reciprocal rank fusion: score = sum of 1 / (RRF_K + rank) over the lists a chunk appears in.
    Without query_text: plain vector search.

    Superseded and expired documents are excluded unless include_superseded=True.
    Each row has: document_id, chunk_index, content, metadata, similarity (cosine), score,
    vec_rank and kw_rank (None when the chunk wasn't in that list).
    """
    import numpy as np

    where = ["true"]
    if not include_superseded:
        where.append("d.status = 'current'")
        where.append("(d.expires is null or d.expires >= current_date)")
    if not include_restricted:
        where.append("not d.restricted")
    filters = " and ".join(where)
    params = {"q": np.asarray(query_vector), "k": k, "n": CANDIDATES, "rrf": RRF_K, "text": query_text or ""}

    if not query_text:
        return conn.execute(
            f"""select c.document_id, c.chunk_index, c.content, c.embed_text, c.metadata,
                       1 - (c.embedding <=> %(q)s) as similarity,
                       1 - (c.embedding <=> %(q)s) as score,
                       row_number() over (order by c.embedding <=> %(q)s) as vec_rank, null::bigint as kw_rank
                from chunks c join documents d on d.id = c.document_id
                where {filters}
                order by c.embedding <=> %(q)s
                limit %(k)s""", params).fetchall()

    # Keyword side, weighted by rarity (IDF, the core of BM25): a word found in 1 chunk ("collection")
    # counts far more than one found in a dozen ("billing"). Postgres's ts_rank_cd ignores rarity.
    # Term counts come from ts_stat over all chunks on every query: fine for a few thousand chunks;
    # for a big corpus, keep them in a table refreshed after each ingest.
    return conn.execute(
        f"""with qterms as (
                select distinct lex from unnest(tsvector_to_array(to_tsvector('english', %(text)s))) as lex
            ),
            total as (select greatest(count(*), 1)::float as n from chunks),
            idf as (
                select s.word, ln(1 + total.n / s.ndoc) as w
                from ts_stat('select to_tsvector(''english'', content) from chunks') s, total
                where s.word in (select lex from qterms)
            ),
            -- Rank inside an outer query so the inner ORDER BY ... LIMIT can use the HNSW index.
            vec as (
                select id, row_number() over (order by dist) as r from (
                    select c.id, c.embedding <=> %(q)s as dist
                    from chunks c join documents d on d.id = c.document_id
                    where {filters}
                    order by c.embedding <=> %(q)s
                    limit %(n)s
                ) v
            ),
            kw as (
                select id, row_number() over (order by score desc, id) as r from (
                    select c.id, sum(idf.w) as score
                    from chunks c join documents d on d.id = c.document_id, idf
                    where {filters}
                      and to_tsvector('english', c.content) @@ plainto_tsquery('simple', idf.word)
                    group by c.id
                    order by score desc
                    limit %(n)s
                ) k
            ),
            fused as (
                select id, sum(1.0 / (%(rrf)s + r)) as score from (
                    select id, r from vec union all select id, r from kw
                ) both_lists group by id
            )
            select c.document_id, c.chunk_index, c.content, c.embed_text, c.metadata,
                   1 - (c.embedding <=> %(q)s) as similarity, f.score,
                   vec.r as vec_rank, kw.r as kw_rank
            from fused f
            join chunks c on c.id = f.id
            left join vec on vec.id = f.id
            left join kw on kw.id = f.id
            order by f.score desc, vec.r nulls last
            limit %(k)s""", params).fetchall()
