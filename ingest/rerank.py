"""Cross-encoder reranking.

Search (vector + keyword) is fast but approximate: it compares a question and a chunk that were each
summarized separately. A cross-encoder reads the question and one chunk *together* and scores how well
that chunk answers the question. It's too slow to run over every chunk, so it runs on the ~30
candidates hybrid search returns and reorders them.

Default model: Xenova/ms-marco-MiniLM-L-6-v2 (~80 MB, runs on CPU, downloaded on first use).
Set RERANK_MODEL in .env to try another (e.g. Xenova/ms-marco-MiniLM-L-12-v2), or RERANKER=none to turn
reranking off.
"""
from __future__ import annotations

import os

DEFAULT_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


class CrossEncoderReranker:
    def __init__(self, model: str | None = None):
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError:
            raise SystemExit("Reranking needs fastembed >= 0.4: pip install -U fastembed "
                             "(or set RERANKER=none in .env)")
        self.model = model or os.environ.get("RERANK_MODEL") or DEFAULT_RERANK_MODEL
        print(f"Loading reranker {self.model} (downloads ~80 MB the first time)...")
        self._m = TextCrossEncoder(model_name=self.model)

    def score(self, question: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self._m.rerank(question, texts)]


def get_reranker(name: str | None = None):
    name = (name or os.environ.get("RERANKER") or "local").lower()
    if name in ("none", "off", "false", "0"):
        return None
    if name in ("local", "cross-encoder"):
        return CrossEncoderReranker()
    raise ValueError(f"unknown reranker: {name} (use 'local' or 'none')")


def rerank(reranker, question: str, rows: list[dict], k: int) -> list[dict]:
    """Score each candidate with the cross-encoder and return the best k, best first.

    Each row keeps its search ranks and gains rerank_score and search_rank (its position before reranking).
    The text scored is embed_text, which carries the "Document / Section" header, so a short clause
    like "6. ... $40 fee" is judged with its context.
    """
    for i, r in enumerate(rows, start=1):
        r["search_rank"] = i
    if reranker is None or not rows:
        return rows[:k]
    scores = reranker.score(question, [r.get("embed_text") or r["content"] for r in rows])
    for r, s in zip(rows, scores):
        r["rerank_score"] = s
    return sorted(rows, key=lambda r: r["rerank_score"], reverse=True)[:k]
