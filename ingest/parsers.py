"""Turn each source file into a ParsedDocument made of Sections.

One parser per format. Each one keeps the document's structure (headings, tables, Q&A pairs) because
the chunker uses that structure to decide where chunks start and end, and every chunk carries its
section path as context.
"""
from __future__ import annotations

import csv
import re
import statistics
from collections import Counter
from pathlib import Path

from .models import ParsedDocument, Section

# --------------------------------------------------------------------------- shared helpers

_WS = re.compile(r"[ \t ]+")


def clean(text: str) -> str:
    """Collapse runs of spaces, trim each line, drop empty lines at the ends."""
    lines = [_WS.sub(" ", ln).strip() for ln in text.replace("\r", "").split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank and out:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


def render_row(header: list[str] | None, row: list[str]) -> str:
    """'Code: 97110 | Description: Therapeutic exercise | ...' — keeps column names next to values."""
    cells = []
    for i, val in enumerate(row):
        val = clean(val or "").replace("\n", " ")
        if not val:
            continue
        name = clean(header[i]).replace("\n", " ") if header and i < len(header) and header[i] else ""
        cells.append(f"{name}: {val}" if name else val)
    return " | ".join(cells)


class SectionBuilder:
    """Collects paragraphs under the current heading path and emits a Section on each new heading."""

    def __init__(self):
        self.sections: list[Section] = []
        self.path: list[tuple[int, str]] = []   # (level, heading)
        self.buf: list[str] = []
        self.pages: list[int] = []
        self.ocr = False

    @property
    def heading_path(self) -> list[str]:
        return [h for _, h in self.path]

    def heading(self, level: int, text: str):
        self.flush()
        while self.path and self.path[-1][0] >= level:
            self.path.pop()
        self.path.append((level, clean(text)))

    def text(self, text: str, page: int | None = None):
        text = clean(text)
        if text:
            self.buf.append(text)
            if page is not None:
                self.pages.append(page)

    def add(self, section: Section):
        """Add a ready-made section (table, Q&A) at the current position."""
        self.flush()
        if not section.heading_path:
            section.heading_path = self.heading_path
        self.sections.append(section)

    def flush(self):
        if self.buf:
            self.sections.append(Section(
                text="\n\n".join(self.buf), heading_path=self.heading_path, kind="text",
                page_start=min(self.pages) if self.pages else None,
                page_end=max(self.pages) if self.pages else None, ocr=self.ocr))
        self.buf, self.pages = [], []


# --------------------------------------------------------------------------- Markdown

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_EMPHASIS = re.compile(r"(\*\*|__)(.+?)\1")


def parse_markdown(path: Path) -> list[Section]:
    b = SectionBuilder()
    para: list[str] = []

    def end_para():
        if para:
            b.text("\n".join(para))
            para.clear()

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        m = _MD_HEADING.match(line)
        if m:
            end_para()
            b.heading(len(m.group(1)), _MD_EMPHASIS.sub(r"\2", m.group(2)))
        elif not line.strip() or re.fullmatch(r"\s*([-*_])\1{2,}\s*", line):   # blank line or horizontal rule
            end_para()
        else:
            para.append(_MD_EMPHASIS.sub(r"\2", line))
    end_para()
    b.flush()
    return b.sections


# --------------------------------------------------------------------------- HTML

# Tags that never hold page content.
_HTML_DROP_TAGS = ["script", "style", "noscript", "svg", "img", "iframe", "form", "button", "input", "link", "meta"]
# class/id fragments that mark site chrome rather than content.
_HTML_CHROME = re.compile(r"cookie|consent|banner|breadcrumb|sidebar|widget|skip-link|top-bar|newsletter|"
                          r"chat-widget|share|social|menu|cta|promo|modal|popup", re.I)
_HTML_CHROME_ROLES = {"dialog", "navigation", "banner", "contentinfo", "complementary", "search"}
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_TEXT_TAGS = {"p", "li", "blockquote", "pre", "dd", "dt"}


def _strip_html_chrome(soup) -> int:
    """Remove navigation, cookie banners, sidebars, footers and scripts. Returns how many elements went."""
    removed = 0
    for tag in soup.find_all(_HTML_DROP_TAGS):
        tag.decompose()
        removed += 1
    for tag in soup.find_all(["nav", "aside"]):
        tag.decompose()
        removed += 1
    # A page-level <header>/<footer> is chrome; one inside <article>/<main> belongs to the content.
    for tag in soup.find_all(["header", "footer"]):
        if not tag.find_parent(["article", "main"]):
            tag.decompose()
            removed += 1
    for tag in soup.find_all(True):
        if getattr(tag, "decomposed", False):   # inside something already removed
            continue
        attrs = getattr(tag, "attrs", None)
        if not attrs:
            continue
        ident = " ".join(attrs.get("class", []) or []) + " " + (attrs.get("id") or "")
        if _HTML_CHROME.search(ident) or attrs.get("role") in _HTML_CHROME_ROLES:
            tag.decompose()
            removed += 1
    return removed


def parse_html(path: Path, warnings: list[str]) -> tuple[list[Section], str | None]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    page_title = soup.title.get_text(strip=True) if soup.title else None
    removed = _strip_html_chrome(soup)
    if removed:
        warnings.append(f"removed {removed} boilerplate HTML elements (nav, banners, sidebars, scripts)")
    root = soup.find("main") or soup.find("article") or soup.body or soup
    b = SectionBuilder()

    def walk(node):
        for child in node.children:
            name = getattr(child, "name", None)
            if name is None:                      # bare text directly inside a container
                txt = str(child).strip()
                if txt and node.name not in ("ul", "ol", "table", "tr"):
                    b.text(txt)
            elif name in _HEADING_TAGS:
                b.heading(_HEADING_TAGS[name], child.get_text(" ", strip=True))
            elif name == "details":                # one FAQ entry: question + answer
                summary = child.find("summary")
                q = summary.get_text(" ", strip=True) if summary else ""
                if summary:
                    summary.extract()
                a = clean(child.get_text("\n", strip=True))
                b.add(Section(text=f"Q: {q}\nA: {a}", heading_path=b.heading_path + [q], kind="faq"))
            elif name == "table":
                rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in child.find_all("tr")]
                rows = [r for r in rows if any(r)]
                if rows:
                    header, body = rows[0], rows[1:]
                    b.add(Section(text="\n".join(render_row(header, r) for r in body), kind="table",
                                  table_header=header, table_rows=body))
            elif name in _BLOCK_TEXT_TAGS:
                prefix = "- " if name == "li" else ""
                b.text(prefix + child.get_text(" ", strip=True))
            else:                                  # div, section, ul, ol, article, span ...
                walk(child)

    walk(root)
    b.flush()
    return b.sections, page_title


# --------------------------------------------------------------------------- CSV

def parse_csv(path: Path, title: str) -> list[Section]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [h.replace("_", " ").strip().capitalize() for h in rows[0]]
    sections = []
    for row in rows[1:]:
        # One row = one record = one section, so "Do you take Kaiser?" retrieves exactly the Kaiser row.
        sections.append(Section(text=render_row(header, row), heading_path=[title], kind="row",
                                table_header=header, table_rows=[row]))
    return sections


# --------------------------------------------------------------------------- DOCX

def parse_docx(path: Path) -> list[Section]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    b = SectionBuilder()
    number = 0
    for el in d.element.body.iterchildren():
        tag = el.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(el, d)
            text = p.text.strip()
            style = (p.style.name if p.style is not None else "") or ""
            if not text:
                continue
            if style == "Title":
                b.heading(1, text)
            elif style.startswith("Heading"):
                level = int(style.split()[-1]) if style.split()[-1].isdigit() else 1
                b.heading(level + 1, text)      # Title is level 1, so Heading 1 sits below it
            elif style.startswith("List Number"):
                number += 1
                b.text(f"{number}. {text}")
                continue
            elif style.startswith("List"):
                b.text(f"- {text}")
            else:
                b.text(text)
            number = 0
        elif tag == "tbl":
            t = Table(el, d)
            rows = [[c.text.strip() for c in r.cells] for r in t.rows]
            rows = [r for r in rows if any(r)]
            if rows:
                header, body = rows[0], rows[1:]
                b.add(Section(text="\n".join(render_row(header, r) for r in body), kind="table",
                              table_header=header, table_rows=body))
    # Header/footer text (contact line, revision date) repeats on every page: deliberately skipped.
    b.flush()
    return b.sections


# --------------------------------------------------------------------------- TXT / email

_EMAIL_HEADER = re.compile(r"^(From|To|Cc|Date|Sent|Subject):\s*(.*)$", re.I)
_QUOTE_START = re.compile(r"^(-{3,}\s*Original Message\s*-{3,}|On .+ wrote:|_{5,}|From: .+ <.+@.+>$)", re.I)


def parse_text(path: Path, warnings: list[str]) -> tuple[list[Section], str | None]:
    lines = path.read_text(encoding="utf-8").replace("\r", "").split("\n")
    headers: dict[str, str] = {}
    i = 0
    while i < len(lines) and _EMAIL_HEADER.match(lines[i]):
        m = _EMAIL_HEADER.match(lines[i])
        headers[m.group(1).lower()] = m.group(2).strip()
        i += 1
    is_email = "from" in headers and "subject" in headers

    body = lines[i:]
    if is_email:
        # Keep only the newest message: quoted earlier replies are drafts that may contradict it.
        cut = next((j for j, ln in enumerate(body) if _QUOTE_START.match(ln.strip())), None)
        if cut is not None:
            quoted = sum(1 for ln in body[cut:] if _QUOTE_START.match(ln.strip()))
            warnings.append(f"dropped {quoted} quoted earlier message(s) from the email thread")
            body = body[:cut]
        sig = next((j for j, ln in enumerate(body) if ln.strip() in ("--", "-- ")), None)
        if sig is not None:
            body = body[:sig]
        body = [ln for ln in body if not ln.lstrip().startswith(">")]

    b = SectionBuilder()
    subject = None
    if is_email:
        subject = re.sub(r"^((re|fw|fwd)\s*:\s*)+", "", headers["subject"], flags=re.I).strip()
        sender = re.sub(r"\s*<.*?>", "", headers.get("from", "")).strip()
        b.heading(1, subject)
        b.text(f"Email from {sender}, sent {headers.get('date', 'unknown date')}.")
    para: list[str] = []
    for ln in body:
        if ln.strip():
            para.append(ln.strip())
        elif para:
            b.text("\n".join(para))
            para = []
    if para:
        b.text("\n".join(para))
    b.flush()
    return b.sections, subject


# --------------------------------------------------------------------------- PDF (text layer + tables + OCR)

HEADER_BAND = 0.06   # top 6% of each page: letterheads and running headers
FOOTER_BAND = 0.07   # bottom 7%: page numbers, form numbers
MIN_TEXT_CHARS = 25  # a page with fewer extractable characters is treated as a scan
OCR_CANDIDATES = [(180, 3), (220, 5), (250, 5), (300, 5)]   # (render dpi, median filter size)
_PAGE_NOISE = re.compile(r"^(page \d+( of \d+)?|\d+)$", re.I)


def _line_style(line: dict) -> tuple[float, bool]:
    chars = [c for c in line.get("chars", []) if c["text"].strip()]
    if not chars:
        return 0.0, False
    size = statistics.median(c["size"] for c in chars)
    bold = all("bold" in c.get("fontname", "").lower() for c in chars)
    return round(size, 1), bold


def _inside(obj: dict, bbox: tuple) -> bool:
    x0, top, x1, bottom = bbox
    cx = (obj["x0"] + obj["x1"]) / 2
    cy = (obj["top"] + obj["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


def _clean_cells(rows: list[list]) -> list[list[str]]:
    return [[clean(c or "").replace("\n", " ") for c in r] for r in rows]


def parse_pdf(path: Path, warnings: list[str]) -> list[Section]:
    import pdfplumber

    b = SectionBuilder()
    with pdfplumber.open(str(path)) as pdf:
        # Body font size = the most common character size outside tables. Bold lines larger than that are headings.
        sizes = Counter()
        for page in pdf.pages:
            tbls = [t.bbox for t in page.find_tables()]
            for c in page.chars:
                if c["text"].strip() and not any(_inside(c, bb) for bb in tbls):
                    sizes[round(c["size"], 1)] += 1
        body_size = sizes.most_common(1)[0][0] if sizes else 10.0
        heading_sizes = sorted({s for s in sizes if s > body_size * 1.05}, reverse=True)

        last_table: dict | None = None   # for joining a table that continues onto the next page
        for pno, page in enumerate(pdf.pages, start=1):
            if len(page.chars) < MIN_TEXT_CHARS:
                _ocr_page(page, pno, b, warnings)
                last_table = None
                continue

            h, w = page.height, page.width
            area = page.crop((0, h * HEADER_BAND, w, h * (1 - FOOTER_BAND)))
            tables = area.find_tables()
            bboxes = [t.bbox for t in tables]
            text_only = area.filter(lambda o: o.get("object_type") != "char" or not any(_inside(o, bb) for bb in bboxes))
            items = [(ln["top"], "line", ln) for ln in text_only.extract_text_lines(return_chars=True)]
            items += [(t.bbox[1], "table", t) for t in tables]
            items.sort(key=lambda x: x[0])

            prev_bottom, prev_size, para = None, None, []
            heading_parts: list[str] = []
            heading_size = None
            first_item = True

            def end_para():
                if para:
                    b.text(" ".join(para), pno)
                    para.clear()

            def end_heading():
                nonlocal heading_parts, heading_size
                if heading_parts:
                    level = heading_sizes.index(heading_size) + 1 if heading_size in heading_sizes else len(heading_sizes) + 1
                    b.heading(level, " ".join(heading_parts))
                    heading_parts, heading_size = [], None

            for top, kind, obj in items:
                if kind == "line":
                    text = obj["text"].strip()
                    if not text or _PAGE_NOISE.match(text):
                        continue
                    size, bold = _line_style(obj)
                    if bold and size > body_size * 1.05:              # heading (may wrap onto 2 lines)
                        end_para()
                        if heading_parts and size != heading_size:
                            end_heading()
                        heading_parts.append(text)
                        heading_size = size
                    else:
                        end_heading()
                        gap = top - prev_bottom if prev_bottom is not None else 0
                        if para and (gap > (prev_size or size) * 0.6 or re.match(r"^(\d+\.|[-•☐])\s", text)):
                            end_para()
                        para.append(text)
                    prev_bottom, prev_size = obj["bottom"], size
                    first_item = False
                else:
                    end_heading()
                    end_para()
                    rows = _clean_cells(obj.extract())
                    rows = [r for r in rows if any(r)]
                    if not rows:
                        continue
                    filled = sum(1 for r in rows for c in r if c) / max(1, sum(len(r) for r in rows))
                    if filled < 0.6:
                        warnings.append(f"page {pno}: skipped a mostly empty form table")
                        continue
                    ncols = len(rows[0])
                    continues = (first_item and last_table is not None and last_table["page"] == pno - 1
                                 and last_table["ncols"] == ncols and rows[0] != last_table["header"])
                    if continues:
                        # Same table carried over the page break: append rows to it, reusing its header.
                        sec = last_table["section"]
                        sec.table_rows.extend(rows)
                        sec.text += "\n" + "\n".join(render_row(sec.table_header, r) for r in rows)
                        sec.page_end = pno
                        warnings.append(f"page {pno}: table continued from page {pno - 1}; joined it and reused its header row")
                        last_table["page"] = pno
                    else:
                        header, body = rows[0], rows[1:]
                        sec = Section(text="\n".join(render_row(header, r) for r in body), kind="table",
                                      table_header=header, table_rows=body, page_start=pno, page_end=pno)
                        b.add(sec)
                        last_table = {"page": pno, "ncols": ncols, "header": header, "section": sec}
                    prev_bottom = obj.bbox[3]
                    first_item = False
            end_heading()
            end_para()
            if not tables:
                last_table = None if last_table and last_table["page"] < pno else last_table
    b.flush()
    return b.sections


def _deskew(img):
    """Find the small rotation (±4°) that makes text rows line up best, using a row-sum projection."""
    import numpy as np
    from PIL import Image

    small = img.copy()
    small.thumbnail((900, 900))
    arr = (np.asarray(small) < 128).astype(np.uint8)
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-4, 4.01, 0.25):
        rot = Image.fromarray(arr * 255).rotate(float(angle), resample=Image.BILINEAR)
        score = float(np.var(np.asarray(rot).sum(axis=1)))
        if score > best_score:
            best_angle, best_score = float(angle), score
    if abs(best_angle) >= 0.25:
        img = img.rotate(best_angle, resample=Image.BICUBIC, fillcolor=255)
    return img, best_angle


def _ocr_page(page, pno: int, b: SectionBuilder, warnings: list[str]):
    import os
    import shutil

    import pytesseract
    from PIL import ImageFilter, ImageOps

    # Find the Tesseract program: TESSERACT_CMD in .env, then PATH, then the usual Windows install folder.
    cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not cmd:
        default_win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(default_win):
            cmd = default_win
    if not cmd:
        raise RuntimeError("Tesseract OCR not found. Install it, or set TESSERACT_CMD in .env to the full path "
                           r"of tesseract.exe (e.g. C:\Program Files\Tesseract-OCR\tesseract.exe).")
    pytesseract.pytesseract.tesseract_cmd = cmd

    # Scans vary a lot, so try a few render resolutions / denoise strengths and keep the one
    # Tesseract is most confident about. Cheap: real knowledge bases have few scanned pages.
    best = None
    for dpi, median in OCR_CANDIDATES:
        img = page.to_image(resolution=dpi).original.convert("L")
        w, h = img.size
        img = img.crop((int(w * 0.03), int(h * HEADER_BAND), w, int(h * (1 - FOOTER_BAND))))  # scanner edge + bands
        img = ImageOps.autocontrast(img.filter(ImageFilter.MedianFilter(median)), cutoff=1)
        img, angle = _deskew(img)
        data = pytesseract.image_to_data(img, config="--psm 4", output_type=pytesseract.Output.DICT)
        confs = [float(c) for c, t in zip(data["conf"], data["text"]) if t.strip() and float(c) >= 0]
        conf = sum(confs) / len(confs) if confs else 0.0
        if best is None or conf > best[0]:
            best = (conf, img, angle)
    mean_conf, img, angle = best
    text = pytesseract.image_to_string(img, config="--psm 4")
    text = re.sub(r"(?m)(^|\d\.\s|\.\s)\|(?=\s)", r"\1I", text)   # Tesseract often reads a lone "I" as "|"
    warnings.append(f"page {pno}: no text layer, OCR used (deskew {angle:+.2f}°, mean confidence {mean_conf:.0f}%)")
    if mean_conf < 70:
        warnings.append(f"page {pno}: low OCR confidence — review this page's chunks by hand")

    # Rebuild paragraphs. Tesseract inserts stray blank lines mid-sentence, so a blank line only ends a
    # paragraph when the text before it ends a sentence or the next line starts a numbered item.
    b.ocr = True
    lines = [ln.strip() for ln in text.split("\n")]
    para: list[str] = []
    title_done, pending_break = False, False
    for ln in lines:
        if not ln or _PAGE_NOISE.match(ln):
            pending_break = True
            continue
        numbered = re.match(r"^\d+\.?\s", ln) is not None
        if para and (numbered or (pending_break and re.search(r"[.!?:]$", para[-1]))):
            b.text(" ".join(para), pno)
            para = []
        pending_break = False
        if not title_done and not para and len(ln) < 70 and not numbered and ln.istitle():
            b.heading(1, ln)
            title_done = True
            continue
        para.append(ln)
    if para:
        b.text(" ".join(para), pno)
    b.flush()
    b.ocr = False


# --------------------------------------------------------------------------- dispatcher

PARSERS = {".md": "markdown", ".markdown": "markdown", ".html": "html", ".htm": "html", ".csv": "csv",
           ".docx": "docx", ".txt": "text", ".pdf": "pdf"}


def parse_file(path: Path, meta: dict) -> ParsedDocument:
    ext = path.suffix.lower()
    if ext not in PARSERS:
        raise ValueError(f"unsupported file type: {path.name}")
    kind = PARSERS[ext]
    warnings: list[str] = []
    found_title = None
    if kind == "markdown":
        sections = parse_markdown(path)
    elif kind == "html":
        sections, found_title = parse_html(path, warnings)
    elif kind == "csv":
        sections = parse_csv(path, meta.get("title") or path.stem)
    elif kind == "docx":
        sections = parse_docx(path)
    elif kind == "text":
        sections, found_title = parse_text(path, warnings)
    else:
        sections = parse_pdf(path, warnings)

    title = meta.get("title") or found_title or path.stem.replace("_", " ")
    if not sections:
        warnings.append("no content extracted")
    return ParsedDocument(doc_id=path.stem, source=path.name, title=title, sections=sections,
                          parser=kind, meta=meta, warnings=warnings)
