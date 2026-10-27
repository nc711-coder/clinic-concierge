"""The clinic documents behind a citation, so a patient can check what the answer is based on.

Each citation carries
  excerpt  the passage the answer came from (shown when the patient clicks the source)
  url      /sources/<file>, the original document, if it may be shown publicly

Only current, unrestricted documents listed in data/manifest.json are served: never an outdated price list,
the exercise handout (restricted: not for individual advice) or any file outside data/raw.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
MANIFEST = RAW.parent / "manifest.json"
EXCERPT_CHARS = 700

# Text files are shown as plain text in the browser rather than downloaded.
MEDIA_TYPES = {".pdf": "application/pdf", ".html": "text/html; charset=utf-8", ".md": "text/plain; charset=utf-8",
               ".txt": "text/plain; charset=utf-8", ".csv": "text/plain; charset=utf-8",
               ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


@lru_cache(maxsize=1)
def public_files() -> frozenset[str]:
    try:
        docs = json.loads(MANIFEST.read_text(encoding="utf-8"))["documents"]
    except (OSError, ValueError, KeyError):
        return frozenset()
    return frozenset(name for name, d in docs.items()
                     if d.get("status") == "current" and not d.get("restricted") and (RAW / name).is_file()
                     and Path(name).suffix.lower() in MEDIA_TYPES)


def source_url(metadata: dict) -> str | None:
    name = metadata.get("source")
    if name not in public_files():
        return None
    page = metadata.get("page_start")
    return f"/sources/{name}" + (f"#page={page}" if page and name.lower().endswith(".pdf") else "")


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    text = re.sub(r"[ \t]+", " ", (text or "")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def path_for(name: str) -> Path | None:
    """The file to serve for /sources/<name>, or None. Only names on the allow-list, so no ../ tricks."""
    return RAW / name if name in public_files() else None
