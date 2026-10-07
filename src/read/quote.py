"""Verified quotes: wording an answer reuses from a section is found by code and shown as a
quotation, with the words sliced from the section's canonical text (CLAUDE.md rule 1).

A sentence that repeats at least NGRAM consecutive words of a section it cites gets that stretch
marked as a quote of that section. A stretch longer than MAX_QUOTE_WORDS, or one taken from a
section the sentence doesn't cite, is a validation problem instead. Matching is on words,
ignoring case and punctuation; the quote shown keeps the source's own punctuation.
"""

import re
from dataclasses import dataclass

NGRAM = 8
MAX_QUOTE_WORDS = 40
TOKEN = re.compile(r"[a-z0-9]+(?:['’][a-z0-9]+)*")
MARKS = "\"'“”‘’"
LINE_HYPHEN = re.compile(r"(?<=[a-z])-[ \t]*\n\s*(?=[a-z])")


@dataclass
class Span:
    """A stretch of a sentence that repeats a section word for word."""

    sent_start: int
    sent_end: int
    sec_start: int
    sec_end: int
    words: int


def tokens(text: str) -> list[tuple[str, int, int]]:
    """(word, start, end) for each word, lowercased; offsets index the original text."""
    low = text.lower()  # same length for the text we handle; offsets stay valid
    return [(m.group().replace("’", "'"), m.start(), m.end()) for m in TOKEN.finditer(low)]


def copied_spans(sentence: str, section_text: str, n: int = NGRAM) -> list[Span]:
    """Maximal stretches of at least n words the sentence shares with the section, in order."""
    st, sc = tokens(sentence), tokens(section_text)
    if len(st) < n or len(sc) < n:
        return []
    index: dict[tuple[str, ...], list[int]] = {}
    for j in range(len(sc) - n + 1):
        index.setdefault(tuple(w for w, _, _ in sc[j : j + n]), []).append(j)
    spans, i = [], 0
    while i <= len(st) - n:
        starts = index.get(tuple(w for w, _, _ in st[i : i + n]))
        if not starts:
            i += 1
            continue
        best_len, best_j = 0, starts[0]
        for j in starts:
            k = n
            while i + k < len(st) and j + k < len(sc) and st[i + k][0] == sc[j + k][0]:
                k += 1
            if k > best_len:
                best_len, best_j = k, j
        spans.append(
            Span(
                st[i][1],
                st[i + best_len - 1][2],
                sc[best_j][1],
                sc[best_j + best_len - 1][2],
                best_len,
            )
        )
        i += best_len
    return spans


def display_quote(raw: str) -> str:
    """Canonical words laid out on one line. A hyphen at a line break is kept and the break
    removed ("instructional-level", "struc-tures"): we can't tell a compound from a split word,
    so no source character is ever dropped."""
    return " ".join(LINE_HYPHEN.sub("-", raw).split())


def quote_parts(sentence: str, cites: list[str], texts: dict[str, str]) -> list[dict]:
    """Split a sentence into own-words text and verified quotes from its cited sections."""
    found: list[tuple[Span, str]] = []
    for c in cites:
        if c in texts:
            found += [(sp, c) for sp in copied_spans(sentence, texts[c])]
    found.sort(key=lambda f: (-f[0].words, f[0].sent_start))
    chosen: list[tuple[Span, str]] = []
    for sp, c in found:  # longest first; skip overlaps
        if all(sp.sent_end <= o.sent_start or sp.sent_start >= o.sent_end for o, _ in chosen):
            chosen.append((sp, c))
    chosen.sort(key=lambda f: f[0].sent_start)
    parts, pos = [], 0
    for sp, c in chosen:
        before = sentence[pos : sp.sent_start]
        if parts or before:
            kept = before.rstrip().rstrip(MARKS).rstrip()
            parts.append({"text": kept + (" " if kept else "")})
        parts.append({"quote": display_quote(texts[c][sp.sec_start : sp.sec_end]), "cite": c})
        pos = sp.sent_end
        while pos < len(sentence) and sentence[pos] in MARKS:  # the model's own closing mark
            pos += 1
    if pos < len(sentence):
        parts.append({"text": sentence[pos:]})
    return [p for p in parts if p.get("quote") or p.get("text")]
