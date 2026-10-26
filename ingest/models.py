"""Data structures shared by the parsers, the chunker and the store."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SectionKind = Literal["text", "table", "faq", "row"]


@dataclass
class Section:
    """A logical block of a document: a run of prose under one heading, a table, a Q&A, a CSV row."""

    text: str
    heading_path: list[str] = field(default_factory=list)
    kind: SectionKind = "text"
    page_start: int | None = None
    page_end: int | None = None
    # Tables only: header row + data rows, so the chunker can repeat the header in every chunk.
    table_header: list[str] | None = None
    table_rows: list[list[str]] | None = None
    ocr: bool = False


@dataclass
class ParsedDocument:
    doc_id: str                 # file name without extension, stable across runs
    source: str                 # file name as it appears in data/raw
    title: str
    sections: list[Section]
    parser: str
    meta: dict = field(default_factory=dict)       # from the manifest
    warnings: list[str] = field(default_factory=list)


@dataclass
class Chunk:
    doc_id: str
    chunk_index: int
    content: str        # what the LLM sees as context
    embed_text: str     # content + a short "Document / Section" header, which is what gets embedded
    metadata: dict
    content_hash: str
