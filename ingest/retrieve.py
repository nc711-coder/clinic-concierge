"""Retrieval entry point used by the search command and the chat endpoint:

    question(s) -> hybrid search (vector + keyword, top CANDIDATES each) -> cross-encoder rerank -> top k
"""
from __future__ import annotations

from . import store
from .rerank import rerank


def retrieve(conn, embedder, reranker, question: str, k: int = 5, *, hybrid: bool = True,
             extra_queries: list[str] | None = None, rerank_query: str | None = None,
             include_superseded: bool = False, include_restricted: bool = True) -> list[dict]:
    """Search for `question` (and any `extra_queries`), merge the candidates, rerank, return the best k.

    The chat endpoint passes the LLM-rewritten query as `question` and the patient's original words in
    `extra_queries`, so a bad rewrite can't hide a chunk the original wording would have found.
    `rerank_query` is what the cross-encoder judges relevance against (default: `question`).
    """
    queries = [question] + [q for q in (extra_queries or []) if q and q.strip() and q != question]
    depth = store.CANDIDATES if reranker is not None else k
    lists = []
    for q in queries:
        lists.append(store.search(conn, embedder.embed_query(q), k=depth, query_text=q if hybrid else None,
                                  include_superseded=include_superseded, include_restricted=include_restricted))

    # Interleave the lists (1st of each, then 2nd of each, ...) and drop duplicates.
    merged, seen = [], set()
    for i in range(max((len(l) for l in lists), default=0)):
        for l in lists:
            if i < len(l):
                key = (l[i]["document_id"], l[i]["chunk_index"])
                if key not in seen:
                    seen.add(key)
                    merged.append(l[i])
    return rerank(reranker, rerank_query or question, merged, k)
