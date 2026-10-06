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


def normalize_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def sniff(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            pass
        raise Unsupported("zip container that is not a Word document")
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


def pdf_lines(page) -> list[Line]:
    """Lines of one pdfplumber page with font size and boldness."""
    out = []
    for raw in page.extract_text_lines(return_chars=True, strip=True):
        chars = [c for c in raw["chars"] if c["text"].strip()]
        text = normalize_newlines(raw["text"]).strip()
        if not chars or not text:
            continue
        sizes = sorted(c["size"] for c in chars)
        out.append(
            Line(
                text=text,
                top=raw["top"],
                bottom=raw["bottom"],
                x0=raw["x0"],
                size=sizes[len(sizes) // 2],
                bold=all(BOLD.search(c.get("fontname", "")) for c in chars),
            )
        )
    return out


def margin_key(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def drop_running_lines(pages: list[tuple[float, list[Line]]]) -> list[list[Line]]:
    """Remove page numbers and headers/footers repeated in the margins of many pages."""
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
        kept.append([ln for ln in lines if id(ln) not in drop])
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
            elif k == 0:  # first line of a page: continue only a sentence cut by the page break
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
