"""Ingestion pipeline: manifest -> parse -> chunk -> embed -> store."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .chunker import chunk_document
from .models import Chunk, ParsedDocument
from .parsers import PARSERS, parse_file

# Bump when parsing or chunking logic changes, so unchanged files get re-processed anyway.
PIPELINE_VERSION = "2"   # 2: numbered lists split into one chunk per item

log = logging.getLogger("ingest")


@dataclass
class DocReport:
    source: str
    status: str                 # ingested | skipped (unchanged) | dry-run | failed
    chunks: int = 0
    embedded: int = 0           # new embeddings requested from the API
    reused: int = 0             # embeddings reused from the database
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("documents", data)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover(data_dir: Path, manifest: dict) -> tuple[list[Path], list[str]]:
    files = sorted(p for p in data_dir.iterdir() if p.is_file() and p.suffix.lower() in PARSERS)
    problems = [f"{p.name}: not in manifest (ingested with default metadata)" for p in files if p.name not in manifest]
    problems += [f"{name}: listed in manifest but file not found" for name in manifest if not (data_dir / name).exists()]
    return files, problems


def parse_and_chunk(path: Path, meta: dict) -> tuple[ParsedDocument, list[Chunk]]:
    doc = parse_file(path, meta)
    return doc, chunk_document(doc)


def run(data_dir: Path, manifest_path: Path, *, embedder=None, database_url: str | None = None,
        dry_run: bool = False, out: Path | None = None, force: bool = False, do_prune: bool = False,
        only: list[str] | None = None) -> tuple[list[DocReport], list[str]]:
    manifest = load_manifest(manifest_path)
    files, problems = discover(data_dir, manifest)
    if only:
        files = [p for p in files if p.name in only or p.stem in only]

    conn = None
    if not dry_run:
        from . import store
        conn = store.connect(database_url, embedder.dims)
        store.check_dims(conn, embedder.dims)

    reports: list[DocReport] = []
    out_f = out.open("w", encoding="utf-8") if out else None
    try:
        for path in files:
            meta = manifest.get(path.name, {})
            sha = file_sha256(path)
            try:
                if conn is not None and not force:
                    existing = store.get_document(conn, path.stem)
                    if existing and existing["file_sha256"] == sha and existing["pipeline_version"] == PIPELINE_VERSION:
                        reports.append(DocReport(path.name, "skipped (unchanged)"))
                        continue

                doc, chunks = parse_and_chunk(path, meta)
                report = DocReport(path.name, "dry-run" if dry_run else "ingested", chunks=len(chunks),
                                   warnings=list(doc.warnings))

                if out_f:
                    for c in chunks:
                        out_f.write(json.dumps({"doc_id": c.doc_id, "chunk_index": c.chunk_index,
                                                "metadata": c.metadata, "embed_text": c.embed_text}) + "\n")

                if conn is not None:
                    cache = store.cached_embeddings(conn, [c.content_hash for c in chunks], embedder.model)
                    todo = [c for c in chunks if c.content_hash not in cache]
                    fresh = embedder.embed([c.embed_text for c in todo]) if todo else []
                    new = {c.content_hash: v for c, v in zip(todo, fresh)}
                    vectors = [cache.get(c.content_hash, new.get(c.content_hash)) for c in chunks]
                    store.replace_document(conn, doc, sha, PIPELINE_VERSION, chunks, vectors, embedder.model)
                    report.embedded, report.reused = len(todo), len(chunks) - len(todo)
                reports.append(report)
            except Exception as e:  # one bad file shouldn't stop the run
                log.exception("failed on %s", path.name)
                reports.append(DocReport(path.name, "failed", error=f"{type(e).__name__}: {e}"))

        if conn is not None and do_prune:
            from . import store
            gone = store.prune(conn, {p.stem for p in files})
            problems += [f"{g}: removed from the database (file no longer in {data_dir})" for g in gone]
    finally:
        if out_f:
            out_f.close()
        if conn is not None:
            conn.close()
    return reports, problems
