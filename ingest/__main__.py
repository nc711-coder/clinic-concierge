"""Command line entry point.

    python -m ingest --dry-run                 # parse + chunk only, write chunks.jsonl, no API or DB
    python -m ingest                           # full run: embed with OpenAI, store in Postgres
    python -m ingest --force                   # re-process every file even if unchanged
    python -m ingest --only 02_faq.html        # just one (or a few) files
    python -m ingest search "do you take kaiser?"
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .pipeline import run

ROOT = Path(__file__).resolve().parent.parent


from .env import load_dotenv  # noqa: E402  (re-exported for backwards compatibility)


def cmd_ingest(args) -> int:
    from .embed import get_embedder

    embedder = None if args.dry_run else get_embedder(args.embedder)
    db = None if args.dry_run else os.environ.get("DATABASE_URL")
    if not args.dry_run and not db:
        print("DATABASE_URL is not set (put it in .env). Use --dry-run to parse and chunk without a database.")
        return 2
    out = Path(args.out) if args.out else (ROOT / "chunks.jsonl" if args.dry_run else None)

    reports, problems = run(Path(args.data_dir), Path(args.manifest), embedder=embedder, database_url=db,
                            dry_run=args.dry_run, out=out, force=args.force, do_prune=args.prune, only=args.only)

    width = max(len(r.source) for r in reports) if reports else 10
    print(f"\n{'file':<{width}}  {'status':<20} {'chunks':>6} {'new emb':>7} {'reused':>6}")
    for r in reports:
        print(f"{r.source:<{width}}  {r.status:<20} {r.chunks:>6} {r.embedded:>7} {r.reused:>6}")
        for w in r.warnings:
            print(f"{'':<{width}}    ! {w}")
        if r.error:
            print(f"{'':<{width}}    ERROR {r.error}")
    for p in problems:
        print(f"! {p}")
    total = sum(r.chunks for r in reports)
    print(f"\n{len(reports)} files, {total} chunks", end="")
    if embedder is not None:
        print(f", {embedder.tokens_used} embedding tokens", end="")
    if out:
        print(f"\nchunks written to {out}", end="")
    print()
    return 1 if any(r.status == "failed" for r in reports) else 0


def cmd_search(args) -> int:
    from . import store
    from .embed import get_embedder
    from .rerank import get_reranker
    from .retrieve import retrieve

    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set (put it in .env).")
        return 2
    embedder = get_embedder(args.embedder)
    reranker = None if args.no_rerank else get_reranker()
    conn = store.connect(os.environ["DATABASE_URL"], embedder.dims)
    store.check_dims(conn, embedder.dims)

    mode = "vector only" if args.vector_only else "hybrid (vector + keyword)"
    mode += " + reranker" if reranker else ""
    print(f"Search mode: {mode}")
    rows = retrieve(conn, embedder, reranker, args.query, k=args.k, hybrid=not args.vector_only,
                    include_superseded=args.include_superseded)
    for r in rows:
        m = r["metadata"]
        where = m.get("section") or m.get("title")
        how = f"vector #{r['vec_rank'] or '-'}, keyword #{r['kw_rank'] or '-'}"
        if "rerank_score" in r:
            head = f"[rerank {r['rerank_score']:+.2f}]"
            how += f", search #{r['search_rank']}"
        else:
            head = f"[{r['similarity']:.3f}]"
        print(f"\n{head} {m['source']} — {where}   ({how})")
        print("   " + r["content"][:300].replace("\n", "\n   "))
    return 0


def main(argv=None) -> int:
    # Windows consoles default to a legacy code page; chunk text has characters like "–" and "☐".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m ingest", description="Ingest clinic documents into pgvector.")
    p.add_argument("--embedder", default=os.environ.get("EMBEDDER", "local"), choices=["local", "openai", "hash"])
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("search", help="query the index")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--include-superseded", action="store_true")
    s.add_argument("--vector-only", action="store_true", help="skip keyword search (to compare)")
    s.add_argument("--no-rerank", action="store_true", help="skip the cross-encoder reranker (to compare)")

    p.add_argument("--data-dir", default=str(ROOT / "data" / "raw"))
    p.add_argument("--manifest", default=str(ROOT / "data" / "manifest.json"))
    p.add_argument("--dry-run", action="store_true", help="parse and chunk only; no API calls, no database")
    p.add_argument("--out", help="write chunks as JSONL here (default chunks.jsonl on --dry-run)")
    p.add_argument("--force", action="store_true", help="re-process files even if unchanged")
    p.add_argument("--prune", action="store_true", help="delete documents whose files were removed")
    p.add_argument("--only", nargs="+", help="only these file names")
    args = p.parse_args(argv)
    return cmd_search(args) if args.command == "search" else cmd_ingest(args)


if __name__ == "__main__":
    sys.exit(main())
