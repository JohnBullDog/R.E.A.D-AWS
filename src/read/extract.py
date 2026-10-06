"""Detect a source file's format by content and extract canonical text.

See docs/design.md, "Format detection and extraction". DOC conversion (LibreOffice) and
Textract for scanned PDFs are deferred for the PoC, so those inputs raise Unsupported.
"""

import io
import zipfile
from dataclasses import dataclass, field

from charset_normalizer import from_bytes

# A PDF whose pages average fewer characters than this is treated as scanned.
MIN_PDF_CHARS_PER_PAGE = 200
TABLE_CELL_SEP = " | "


class Unsupported(Exception):
    """The file can't be ingested by the PoC extractors; the message says why."""


@dataclass
class Extracted:
    text: str  # canonical text: UTF-8 when written, "\n" line endings only
    page_starts: list[int] = field(default_factory=list)  # char offset where each page begins
    headings: set[str] = field(default_factory=set)  # paragraphs the source marks as headings
    kind: str = ""


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


def extract_pdf(data: bytes) -> Extracted:
    import pdfplumber

    parts: list[str] = []
    page_starts: list[int] = []
    pos = 0
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            page_starts.append(pos)
            part = normalize_newlines(page.extract_text() or "") + "\n\n"
            parts.append(part)
            pos += len(part)
    text = "".join(parts)
    if page_starts and len(text.strip()) / len(page_starts) < MIN_PDF_CHARS_PER_PAGE:
        raise Unsupported("PDF has little or no text layer (scanned?); Textract is deferred")
    return Extracted(text=text, page_starts=page_starts, kind="pdf")


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
