"""Split canonical text into sections (display unit) and passages (search unit).

Every record carries exact character offsets into the canonical text, so excerpts can be
sliced and hash-checked later without any text passing through a model. See docs/design.md,
"Chunker".
"""

import bisect
import hashlib
import re
from collections.abc import Iterator

HEADING = re.compile(r"(chapter|part|book|section)\s+\S+", re.I)
PARAGRAPH = re.compile(r"[^\n](?:.|\n(?!\s*\n))*")
SENTENCE_WORD = re.compile(r"[.!?][\"'”’)\]]*$")  # a word that ends a sentence
WORD = re.compile(r"\S+")

TARGET_WORDS = 250  # passage size
MAX_SECTION_WORDS = 1500  # a section without headings closes after this many words
MAX_PARAGRAPH_WORDS = 400  # longer paragraphs are split at sentence boundaries


class ChunkError(Exception):
    """Raised when a record's offsets don't reproduce its text. Ingestion must fail."""


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def paragraphs(text: str) -> Iterator[tuple[int, int]]:
    """Yield exact (start, end) offsets of each blank-line-separated paragraph, trimmed."""
    for m in PARAGRAPH.finditer(text):
        s, e = m.start(), m.end()
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            yield s, e


def split_long(text: str, s: int, e: int, max_words: int) -> Iterator[tuple[int, int]]:
    """Split text[s:e] into pieces of at most max_words, at sentence boundaries when possible.

    A single sentence longer than max_words is split at word boundaries. Pieces are trimmed
    of surrounding whitespace, so their offsets still reproduce the text exactly.
    """
    words = [(m.start(), m.end()) for m in WORD.finditer(text, s, e)]
    if len(words) <= max_words:
        yield s, e
        return
    first = 0  # index of the first word of the current piece
    last_break = None  # index of the last sentence-ending word in the current piece
    for i, (ws, we) in enumerate(words):
        if SENTENCE_WORD.search(text, ws, we):
            last_break = i
        if i - first + 1 >= max_words:
            cut = last_break if last_break is not None else i
            yield words[first][0], words[cut][1]
            first, last_break = cut + 1, None
            # words after the cut that were already scanned may end a sentence
            for j in range(first, i + 1):
                if SENTENCE_WORD.search(text, words[j][0], words[j][1]):
                    last_break = j
    if first < len(words):
        yield words[first][0], words[-1][1]


def units(text: str, max_words: int = MAX_PARAGRAPH_WORDS) -> Iterator[tuple[int, int]]:
    """Paragraphs, with over-long ones split into sentence-bounded pieces."""
    for s, e in paragraphs(text):
        yield from split_long(text, s, e, max_words)


def page_at(page_starts: list[int], offset: int) -> int | None:
    """1-based page number containing offset, or None when the source has no pages."""
    if not page_starts:
        return None
    return bisect.bisect_right(page_starts, offset)


def build_chunks(
    work_id: str,
    ver: str,
    text: str,
    headings: frozenset[str] | set[str] = frozenset(),
    page_starts: list[int] | None = None,
    target: int = TARGET_WORDS,
    max_sec: int = MAX_SECTION_WORDS,
) -> tuple[list[dict], list[dict]]:
    """Return (sections, passages) for one canonical text. Passages never cross a section."""
    pages = page_starts or []
    sections: list[dict] = []
    passages: list[dict] = []
    sec: list | None = None  # [start, end, words, heading]
    psg: list | None = None  # [start, end, words]

    def section_id() -> str:
        return f"{work_id}:{ver}:s{len(sections):04d}"

    def close_passage() -> None:
        nonlocal psg
        if psg:
            s, e = psg[0], psg[1]
            passages.append(
                {
                    "chunk_id": f"{work_id}:{ver}:p{len(passages):05d}",
                    "section_id": section_id(),
                    "work_id": work_id,
                    "version_id": ver,
                    "char_start": s,
                    "char_end": e,
                    "text": text[s:e],
                    "text_sha256": sha(text[s:e]),
                    "section_path": sec[3] if sec else None,
                    "page": page_at(pages, s),
                }
            )
            psg = None

    def close_section() -> None:
        nonlocal sec
        close_passage()
        if sec:
            s, e = sec[0], sec[1]
            sections.append(
                {
                    "section_id": section_id(),
                    "work_id": work_id,
                    "version_id": ver,
                    "char_start": s,
                    "char_end": e,
                    "section_path": sec[3],
                    "page": page_at(pages, s),
                    "text_sha256": sha(text[s:e]),
                }
            )
            sec = None

    for s, e in units(text):
        para = text[s:e]
        words = len(para.split())
        is_heading = para in headings or (len(para) < 100 and bool(HEADING.match(para)))
        if is_heading or (sec and sec[2] + words > max_sec):
            close_section()
        if not sec:
            sec = [s, e, 0, para if is_heading else None]
        sec[1], sec[2] = e, sec[2] + words
        if psg and psg[2] + words > target:
            close_passage()
        if not psg:
            psg = [s, e, 0]
        psg[1], psg[2] = e, psg[2] + words
    close_section()

    verify(text, sections, passages)
    return sections, passages


def verify(text: str, sections: list[dict], passages: list[dict]) -> None:
    """Assert every offset reproduces its text and every passage sits inside its section."""
    by_id = {s["section_id"]: s for s in sections}
    for p in passages:
        if text[p["char_start"] : p["char_end"]] != p["text"] or sha(p["text"]) != p["text_sha256"]:
            raise ChunkError(f"passage {p['chunk_id']} offsets don't reproduce its text")
        sec = by_id.get(p["section_id"])
        if sec is None or not (
            sec["char_start"] <= p["char_start"] <= p["char_end"] <= sec["char_end"]
        ):
            raise ChunkError(f"passage {p['chunk_id']} crosses its section boundary")
    for s in sections:
        if sha(text[s["char_start"] : s["char_end"]]) != s["text_sha256"]:
            raise ChunkError(f"section {s['section_id']} offsets don't reproduce its hash")
