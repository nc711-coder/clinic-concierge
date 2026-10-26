"""Split parsed sections into retrieval-sized chunks.

Rules:
- A section that fits stays whole (one FAQ answer, one policy clause, one CSV row = one chunk).
- Long prose is split on paragraph, then sentence boundaries, with a small overlap between chunks.
- Tables are split by rows; every row already carries its column names ("Code: 97110 | Charge: $62").
- Every chunk is embedded with a "Document / Section" header so a chunk like "Fee: $40" still knows
  it belongs to the cancellation policy.
"""
from __future__ import annotations

import hashlib
import re

from .models import Chunk, ParsedDocument, Section
from .parsers import render_row

MAX_CHARS = 1200      # ~300 tokens: big enough for a full policy clause, small enough to stay on-topic
OVERLAP_CHARS = 150   # carried from the end of one prose chunk into the next
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])")


def _split_long(text: str, max_chars: int) -> list[str]:
    """Pack paragraphs (then sentences, then words) into pieces no longer than max_chars."""
    units: list[tuple[str, bool]] = []          # (text, starts a new paragraph)
    for para in text.split("\n\n"):
        if len(para) <= max_chars:
            units.append((para, True))
            continue
        first = True
        for sent in _SENTENCE_END.split(para):
            while len(sent) > max_chars:          # a single giant sentence: hard-wrap on a space
                cut = sent.rfind(" ", 0, max_chars)
                cut = cut if cut > 0 else max_chars
                units.append((sent[:cut], first))
                first = False
                sent = sent[cut:].lstrip()
            if sent:
                units.append((sent, first))
                first = False

    pieces: list[str] = []
    current = ""
    for unit, new_para in units:
        joiner = "\n\n" if new_para else " "
        candidate = f"{current}{joiner}{unit}" if current else unit
        if len(candidate) <= max_chars:
            current = candidate
            continue
        pieces.append(current)
        # Overlap: start the next piece with the tail sentences of the previous one.
        tail = ""
        for sent in reversed(_SENTENCE_END.split(current)):
            if len(tail) + len(sent) + 1 > OVERLAP_CHARS:
                break
            tail = f"{sent} {tail}".strip()
        current = f"{tail} {unit}".strip() if tail and len(tail) + len(unit) + 1 <= max_chars else unit
    if current:
        pieces.append(current)
    return pieces


_NUMBERED = re.compile(r"^\d{1,2}\.?\s+\S")   # "4. I understand..." (OCR sometimes drops the dot: "7 I authorize")
MIN_LIST_ITEMS = 3


def _numbered_items(text: str) -> list[str] | None:
    """If the section is mostly a numbered list, return intro + one piece per item; otherwise None."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    numbered = [p for p in paras if _NUMBERED.match(p)]
    if len(numbered) < MIN_LIST_ITEMS:
        return None
    pieces, intro = [], []
    for p in paras:
        if _NUMBERED.match(p):
            if intro:                       # text before the first item (or between items) stays together
                pieces.append("\n\n".join(intro))
                intro = []
            pieces.append(p)
        else:
            intro.append(p)
    if intro:
        pieces.append("\n\n".join(intro))
    return pieces


def _table_pieces(section: Section, max_chars: int) -> list[str]:
    rows = [render_row(section.table_header, r) for r in (section.table_rows or [])]
    rows = [r for r in rows if r]
    pieces, current = [], ""
    for row in rows:
        candidate = f"{current}\n{row}" if current else row
        if len(candidate) > max_chars and current:
            pieces.append(current)
            current = row
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def chunk_document(doc: ParsedDocument, max_chars: int = MAX_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in doc.sections:
        if section.kind == "table":
            pieces = _table_pieces(section, max_chars)
        elif (items := _numbered_items(section.text)) is not None:
            # A numbered list of separate clauses: one clause per chunk, so each embedding is about one thing.
            pieces = [p for item in items for p in ([item] if len(item) <= max_chars else _split_long(item, max_chars))]
        elif len(section.text) <= max_chars:
            pieces = [section.text]
        else:
            pieces = _split_long(section.text, max_chars)

        # Section path without a repeat of the document title.
        path = [h for h in section.heading_path if h.strip().lower() != doc.title.strip().lower()]
        header = f"Document: {doc.title}"
        if path:
            header += f"\nSection: {' > '.join(path)}"
        if section.kind == "table" and len(pieces) > 1:
            header += " (table, continued)" if path else "\nSection: table (continued)"

        for i, piece in enumerate(pieces):
            piece = piece.strip()
            if not piece:
                continue
            embed_text = f"{header}\n\n{piece}"
            meta = {
                "source": doc.source,
                "title": doc.title,
                "section": " > ".join(path) or None,
                "kind": section.kind,
                "page_start": section.page_start,
                "page_end": section.page_end,
                "ocr": section.ocr,
                "part": f"{i + 1}/{len(pieces)}" if len(pieces) > 1 else None,
                "doc_type": doc.meta.get("doc_type"),
                "status": doc.meta.get("status", "current"),
                "effective_date": doc.meta.get("effective_date"),
                "expires": doc.meta.get("expires"),
                "superseded_by": doc.meta.get("superseded_by"),
                "restricted": bool(doc.meta.get("restricted", False)),
            }
            meta = {k: v for k, v in meta.items() if v is not None}
            chunks.append(Chunk(
                doc_id=doc.doc_id, chunk_index=len(chunks), content=piece, embed_text=embed_text,
                metadata=meta, content_hash=hashlib.sha256(embed_text.encode()).hexdigest()))
    return chunks
