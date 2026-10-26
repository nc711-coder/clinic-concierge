"""Download and load the local embedding and reranking models once, so they're baked into the Docker image
(the first visitor of the live demo shouldn't wait for an 80-130 MB download).

    python -m app.warm
"""
from __future__ import annotations

import os
import time


def main() -> int:
    from ingest.embed import get_embedder
    from ingest.rerank import get_reranker
    start = time.time()
    if os.environ.get("EMBEDDER", "local") == "local":
        emb = get_embedder("local")
        emb.embed_query("warm up")
        print(f"embedding model ready: {emb.model} ({emb.dims} dims)")
    rr = get_reranker()
    if rr is not None:
        rr.score("warm up", ["a short test passage"])
        print(f"reranker ready: {rr.model}")
    print(f"done in {time.time() - start:.1f}s; cache: {os.environ.get('FASTEMBED_CACHE_PATH', '(fastembed default)')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
