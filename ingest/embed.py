"""Embedding providers.

- LocalEmbedder (default): free, runs on your own CPU. Downloads a small open-source model
  (BAAI/bge-small-en-v1.5, ~70 MB, 384 dimensions) on first use and caches it. No API key.
- OpenAIEmbedder: text-embedding-3-small via the OpenAI API (1536 dimensions). Needs OPENAI_API_KEY.
- HashEmbedder: deterministic fake vectors with no meaning, for tests and CI.

Each embedder has embed() for documents and embed_query() for search questions. Some models
(including bge) embed questions slightly differently from passages, which improves retrieval.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
import time

LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIMS = {"local": 384, "openai": 1536, "hash": 384}


class LocalEmbedder:
    def __init__(self, model: str | None = None):
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise SystemExit("The local embedder needs fastembed: pip install fastembed")
        # Separate setting from OpenAI's EMBEDDING_MODEL, so an old .env can't point this at an OpenAI model.
        self.model = model or os.environ.get("LOCAL_EMBEDDING_MODEL") or LOCAL_MODEL
        print(f"Loading local embedding model {self.model} (downloads ~70 MB the first time)...")
        self._m = TextEmbedding(model_name=self.model)
        self.dims = len(next(iter(self._m.passage_embed(["dimension check"]))))
        self.tokens_used = 0   # local: nothing to pay for

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._m.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._m.query_embed([text]))).tolist()


class OpenAIEmbedder:
    def __init__(self, model: str | None = None, dims: int | None = None, batch_size: int = 96):
        from openai import OpenAI

        self.model = model or os.environ.get("EMBEDDING_MODEL") or "text-embedding-3-small"
        self.dims = dims or int(os.environ.get("EMBEDDING_DIMS") or DEFAULT_DIMS["openai"])
        self.batch_size = batch_size
        self.client = OpenAI(max_retries=5)   # reads OPENAI_API_KEY; retries 429s and 5xx with backoff
        self.tokens_used = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            for attempt in range(3):
                try:
                    resp = self.client.embeddings.create(model=self.model, input=batch, dimensions=self.dims)
                    break
                except Exception:   # network blips beyond the client's own retries
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
            self.tokens_used += resp.usage.total_tokens
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


class HashEmbedder:
    """Deterministic unit vectors derived from a hash of the text. No network, no meaning."""

    model = "hash-test"

    def __init__(self, dims: int = DEFAULT_DIMS["hash"]):
        self.dims = dims
        self.tokens_used = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            seed = hashlib.sha256(text.encode()).digest()
            raw, counter = [], 0
            while len(raw) < self.dims:
                block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
                raw.extend(v / 2**31 for v in struct.unpack(">8i", block))
                counter += 1
            raw = raw[: self.dims]
            norm = math.sqrt(sum(v * v for v in raw)) or 1.0
            vectors.append([v / norm for v in raw])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def get_embedder(name: str):
    if name == "local":
        return LocalEmbedder()
    if name == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY is not set (put it in .env), or use the free local embedder: "
                             "set EMBEDDER=local in .env.")
        return OpenAIEmbedder()
    if name == "hash":
        return HashEmbedder(int(os.environ.get("EMBEDDING_DIMS") or DEFAULT_DIMS["hash"]))
    raise ValueError(f"unknown embedder: {name} (use 'local', 'openai' or 'hash')")
