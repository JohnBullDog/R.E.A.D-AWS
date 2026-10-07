"""Detect a source file's format by content and extract canonical text.

See docs/design.md, "Format detection and extraction". DOC conversion (LibreOffice) and
Textract for scanned PDFs are deferred for the PoC, so those inputs raise Unsupported.
"""

import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field

from charset_normalizer import from_bytes

# A PDF whose pages average fewer characters than this is treated as scanned.
MIN_PDF_CHARS_PER_PAGE = 200
TABLE_CELL_SEP = " | "

BOLD = re.compile(r"bold|black|heavy|semibold|demi", re.I)
PAGE_NUMBER = re.compile(r"^\W*(page\s*)?\d+(\s*(of|/)\s*\d+)?\W*$", re.I)
SENTENCE_END = re.compile(r"[.!?:\"”)]$")
MARGIN = 0.08  # top/bottom share of the page where running headers and footers live
HEADING_SIZE_RATIO = 1.15  # a line this much larger than body text can be a heading
MAX_HEADING_CHARS = 120
GUTTER_GAP = 8.0  # min horizontal gap (pt) between a left and right column on one line
MIN_SPLIT_LINES = 5  # a page needs this many split lines to count as two-column
TOC_LINE = re.compile(r"\.{4,}\s*\d+\s*$")  # "Introduction ........ 3"


class Unsupported(Exception):
    """The file can't be ingested by the PoC extractors; the message says why."""


@dataclass
class Extracted:
    text: str  # canonical text: UTF-8 when written, "\n" line endings only
    page_starts: list[int] = field(default_factory=list)  # char offset where each page begins
    headings: set[str] = field(default_factory=set)  # paragraphs the source marks as headings
    kind: str = ""


@dataclass
class Line:
    """One text line on a PDF page, with the layout facts used to rebuild paragraphs."""

    text: str
    top: float
    bottom: float
    x0: float
    size: float  # median font size of the line's characters
    bold: bool  # every visible character is in a bold/semibold font
    col_start: bool = False  # first line of the right column in a two-column region


def normalize_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def sniff(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                if "word/document.xml" in names:
                    return "docx"
                if "ppt/presentation.xml" in names:
                    return "pptx"
        except zipfile.BadZipFile:
            pass
        raise Unsupported("zip container that is not a Word or PowerPoint document")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "doc"
    return "txt"


def extract(data: bytes) -> Extracted:
    kind = sniff(data)
    if kind == "txt":
        return extract_txt(data)
    if kind == "pdf":
        return extract_pdf(data)
    if kind == "docx":
        return extract_docx(data)
    if kind == "pptx":
        return extract_pptx(data)
    raise Unsupported("legacy .doc needs LibreOffice conversion (deferred); save it as .docx")


def extract_txt(data: bytes) -> Extracted:
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise Unsupported("binary file, not text")
    try:
        decoded = data.decode("utf-8-sig")  # valid UTF-8 is never second-guessed
    except UnicodeDecodeError:
        best = from_bytes(data).best()
        if best is None:
            raise Unsupported("not decodable as text") from None
        decoded = str(best)
    text = normalize_newlines(decoded).lstrip("﻿")
    return Extracted(text=text, kind="txt")


def chars_text(chars: list[dict]) -> str:
    """Rebuild text from characters in reading order (used for lines split at a gutter).

    A space goes where the gap exceeds 30% of the median character width, which works in any
    coordinate units; a glyph drawn twice at the same spot (some PDFs do) is kept once.
    """
    ordered = sorted(chars, key=lambda c: c["x0"])
    widths = sorted(c["x1"] - c["x0"] for c in ordered)
    space = 0.3 * widths[len(widths) // 2]
    out = ""
    prev = None
    for c in ordered:
        if prev is not None:
            if c["text"] == prev["text"] and abs(c["x0"] - prev["x0"]) < space:
                continue
            if c["x0"] - prev["x1"] > space and not out.endswith(" "):
                out += " "
        out += c["text"]
        prev = c
    return " ".join(out.split())


def make_line(chars: list[dict], top: float, bottom: float, text: str | None = None) -> Line:
    """A Line from characters; text defaults to a rebuild from the characters."""
    sizes = sorted(c["size"] for c in chars)
    return Line(
        text=normalize_newlines(text if text is not None else chars_text(chars)).strip(),
        top=top,
        bottom=bottom,
        x0=min(c["x0"] for c in chars),
        size=sizes[len(sizes) // 2],
        bold=all(BOLD.search(c.get("fontname", "")) for c in chars),
    )


def split_at(chars: list[dict], x: float) -> tuple[list[dict], list[dict], bool]:
    """(left, right, crosses): crosses is True when text runs across x without a gutter gap."""
    left = [c for c in chars if c["x1"] <= x]
    right = [c for c in chars if c["x0"] >= x]
    if len(left) + len(right) < len(chars):
        return left, right, True  # a character straddles x
    if left and right:
        gap = min(c["x0"] for c in right) - max(c["x1"] for c in left)
        return left, right, gap < GUTTER_GAP
    return left, right, False


def find_gutter(rows: list[list[dict]], width: float) -> float | None:
    """x of the gap between two text columns, or None for a one-column page."""
    best, best_split = None, 0
    for x in range(int(width * 0.35), int(width * 0.65), 2):
        split = cross = 0
        for chars in rows:
            left, right, crosses = split_at(chars, x)
            if crosses:
                cross += 1
            elif left and right:
                split += 1
        if split >= MIN_SPLIT_LINES and split >= 2 * cross and split > best_split:
            best, best_split = float(x), split
    return best


def pdf_lines(page) -> list[Line]:
    """Lines of one pdfplumber page, in reading order, with font size and boldness.

    On a two-column page, lines that run across both columns are split at the gutter and
    each column is read top to bottom; full-width lines (titles, figures) stay in place.
    """
    raws = []
    for raw in page.extract_text_lines(return_chars=True, strip=True):
        chars = [c for c in raw["chars"] if c["text"].strip()]
        if chars and raw["text"].strip():
            raws.append((raw, chars))
    gutter = find_gutter([chars for _, chars in raws], page.width)
    if gutter is None:
        return [make_line(chars, raw["top"], raw["bottom"], raw["text"]) for raw, chars in raws]
    out: list[Line] = []
    left_col: list[Line] = []
    right_col: list[Line] = []

    def flush() -> None:
        out.extend(left_col)
        for k, ln in enumerate(right_col):
            ln.col_start = k == 0 and bool(left_col)
            out.append(ln)
        left_col.clear()
        right_col.clear()

    for raw, chars in raws:
        left, right, crosses = split_at(chars, gutter)
        if crosses:  # full-width line: ends the two-column region above it
            flush()
            out.append(make_line(chars, raw["top"], raw["bottom"], raw["text"]))
            continue
        if left:
            left_col.append(make_line(left, raw["top"], raw["bottom"]))
        if right:
            right_col.append(make_line(right, raw["top"], raw["bottom"]))
    flush()
    return out


def margin_key(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def drop_running_lines(pages: list[tuple[float, list[Line]]]) -> list[list[Line]]:
    """Remove page numbers, headers/footers repeated in the margins of many pages, table-of-
    contents lines, and single-character lines (decorative cover letters, drop caps)."""
    in_margin = [
        [ln for ln in lines if ln.top < h * MARGIN or ln.bottom > h * (1 - MARGIN)]
        for h, lines in pages
    ]
    seen = Counter(k for m in in_margin for k in {margin_key(ln.text) for ln in m})
    repeated = {k for k, n in seen.items() if n >= max(3, len(pages) // 2)}
    kept = []
    for (_h, lines), margin in zip(pages, in_margin, strict=True):
        drop = {
            id(ln) for ln in margin if margin_key(ln.text) in repeated or PAGE_NUMBER.match(ln.text)
        }
        kept.append(
            [
                ln
                for ln in lines
                if id(ln) not in drop and len(ln.text) > 1 and not TOC_LINE.search(ln.text)
            ]
        )
    return kept


def layout_text(pages: list[tuple[float, list[Line]]]) -> tuple[str, list[int], set[str]]:
    """Rebuild paragraphs and headings from line geometry: (text, page_starts, headings).

    Paragraphs are separated by a blank line and keep their internal line breaks. A heading
    is a short line noticeably larger than body text, or entirely bold. A paragraph cut by a
    page break stays one paragraph; page_starts then points into the middle of it.
    """
    pages_lines = drop_running_lines(pages)
    all_lines = [ln for lines in pages_lines for ln in lines]
    if not all_lines:
        return "", [0] * len(pages), set()
    weight: Counter = Counter()
    for ln in all_lines:
        weight[round(ln.size, 1)] += len(ln.text)
    body = weight.most_common(1)[0][0]
    gaps = sorted(
        b.top - a.bottom
        for lines in pages_lines
        for a, b in zip(lines, lines[1:], strict=False)
        if abs(a.size - body) < 0.5 and abs(b.size - body) < 0.5 and b.top > a.bottom
    )
    # Typical gap between lines inside a paragraph. The lower quartile, capped at 60% of the
    # font size, so pages of one-line paragraphs (lists) can't pass off paragraph gaps as it.
    line_gap = min(gaps[len(gaps) // 4] if gaps else body, body * 0.6)

    def is_heading(ln: Line) -> bool:
        return (
            len(ln.text) <= MAX_HEADING_CHARS
            and len(re.findall(r"[^\W\d_]", ln.text)) >= 3
            and not ln.text.endswith((".", ",", ";"))
            and (ln.size >= body * HEADING_SIZE_RATIO or ln.bold)
        )

    out: list[str] = []
    length = 0
    page_starts: list[int] = []
    headings: set[str] = set()
    para: list[str] = []
    para_heading = False
    prev: Line | None = None

    def next_offset() -> int:
        """Offset where the next appended line will land."""
        if para:
            return length + (2 if out else 0) + sum(len(x) + 1 for x in para)
        return length + (2 if out else 0)

    def flush() -> None:
        nonlocal para, para_heading, length
        if para:
            text = (" " if para_heading else "\n").join(para)
            if para_heading:
                headings.add(text)
            piece = ("\n\n" if out else "") + text
            out.append(piece)
            length += len(piece)
        para, para_heading = [], False

    for lines in pages_lines:
        for k, ln in enumerate(lines):
            head = is_heading(ln)
            if not para or prev is None:
                new_para = True
            elif k == 0 or ln.col_start:  # page or column break: continue only a cut sentence
                new_para = (
                    head
                    or para_heading
                    or bool(SENTENCE_END.search(para[-1]))
                    or abs(ln.size - prev.size) > 0.5
                )
            elif head and para_heading:  # a heading wrapped onto a second line
                new_para = abs(ln.size - prev.size) > 0.5 or ln.top - prev.bottom > ln.size
            else:
                new_para = (
                    head
                    or para_heading
                    or abs(ln.size - prev.size) > 0.5
                    or ln.top - prev.bottom > line_gap * 1.5 + 0.5
                )
            if new_para:
                flush()
            if k == 0:
                page_starts.append(next_offset())
            para.append(ln.text)
            para_heading = head if len(para) == 1 else para_heading and head
            prev = ln
        if not lines:
            page_starts.append(next_offset())
    flush()
    return "".join(out), page_starts, headings


def extract_pdf(data: bytes) -> Extracted:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [(page.height, pdf_lines(page)) for page in pdf.pages]
    text, page_starts, headings = layout_text(pages)
    if pages and len(text.strip()) / len(pages) < MIN_PDF_CHARS_PER_PAGE:
        raise Unsupported("PDF has little or no text layer (scanned?); Textract is deferred")
    return Extracted(text=text, page_starts=page_starts, headings=headings, kind="pdf")


def extract_docx(data: bytes) -> Extracted:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(data))
    paras: list[str] = []
    headings: set[str] = set()
    for block in d.iter_inner_content():  # paragraphs and tables in document order
        if isinstance(block, Paragraph):
            t = normalize_newlines(block.text).strip()
            if not t:
                continue
            paras.append(t)
            if block.style is not None and block.style.name.startswith("Heading"):
                headings.add(t)
        elif isinstance(block, Table):
            for row in block.rows:  # tables are flattened row by row
                cells = [" ".join(normalize_newlines(c.text).split()) for c in row.cells]
                row_text = TABLE_CELL_SEP.join(cells).strip()
                if row_text.strip(TABLE_CELL_SEP.strip() + " "):
                    paras.append(row_text)
    return Extracted(text="\n\n".join(paras), headings=headings, kind="docx")


def extract_pptx(data: bytes) -> Extracted:
    """Slides in order: each slide's title is a heading; text boxes, then table rows, then
    speaker notes. page_starts marks where each slide begins (page = slide number)."""
    from pptx import Presentation

    deck = Presentation(io.BytesIO(data))
    parts: list[str] = []
    page_starts: list[int] = []
    headings: set[str] = set()
    pos = 0
    for n, slide in enumerate(deck.slides, 1):
        paras: list[str] = []
        title = slide.shapes.title
        title_text = " ".join(normalize_newlines(title.text).split()) if title is not None else ""
        paras.append(title_text or f"Slide {n}")
        headings.add(paras[0])
        for shape in slide.shapes:
            if title is not None and shape.shape_id == title.shape_id:
                continue
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    t = " ".join(normalize_newlines("".join(r.text for r in p.runs)).split())
                    if t:
                        paras.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    cells = [" ".join(normalize_newlines(c.text).split()) for c in row.cells]
                    row_text = TABLE_CELL_SEP.join(cells).strip()
                    if row_text.strip(TABLE_CELL_SEP.strip() + " "):
                        paras.append(row_text)
        if slide.has_notes_slide:
            notes = normalize_newlines(slide.notes_slide.notes_text_frame.text).strip()
            if notes:
                paras.append("Speaker notes: " + " ".join(notes.split()))
        piece = ("\n\n" if parts else "") + "\n\n".join(paras)
        page_starts.append(pos + (2 if parts else 0))
        parts.append(piece)
        pos += len(piece)
    return Extracted(text="".join(parts), page_starts=page_starts, headings=headings, kind="pptx")
