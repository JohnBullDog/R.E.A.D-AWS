import io
import sys
import types
import zipfile

import docx
import pytest

from read.chunk import build_chunks
from read.extract import Line, Unsupported, extract, layout_text, sniff


def make_docx() -> bytes:
    d = docx.Document()
    d.add_heading("Recommendation 1", level=1)
    d.add_paragraph("Teach students to recognize the sounds in words.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Grade", "Skill"
    t.cell(1, 0).text, t.cell(1, 1).text = "K", "Blending"
    d.add_paragraph("   ")
    d.add_heading("Recommendation 2", level=1)
    d.add_paragraph("Read connected text daily.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_sniff_by_content_not_extension():
    assert sniff(b"%PDF-1.7 ...") == "pdf"
    assert sniff(make_docx()) == "docx"
    assert sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest") == "doc"
    assert sniff(b"plain words") == "txt"


def test_non_word_zip_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(Unsupported):
        sniff(buf.getvalue())


def test_doc_rejected_while_libreoffice_deferred():
    with pytest.raises(Unsupported, match="docx"):
        extract(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)


def test_txt_normalizes_newlines_and_bom():
    data = "﻿Line one\r\nLine two\rLine three — café".encode()
    ex = extract(data)
    assert ex.text == "Line one\nLine two\nLine three — café"
    assert "\r" not in ex.text


def test_txt_detects_windows_1252():
    para = (
        "Teachers “model” each sound — then students blend them. "
        "Students can’t skip this step; it’s the foundation for decoding. "
    )
    data = (para * 4).encode("cp1252")
    text = extract(data).text
    assert "“model”" in text and "—" in text and "can’t" in text


def test_binary_rejected():
    with pytest.raises(Unsupported):
        extract(b"\x00\x01\x02\x03" * 200)


def test_docx_headings_tables_order():
    ex = extract(make_docx())
    assert ex.text.split("\n\n") == [
        "Recommendation 1",
        "Teach students to recognize the sounds in words.",
        "Grade | Skill",
        "K | Blending",
        "Recommendation 2",
        "Read connected text daily.",
    ]
    assert ex.headings == {"Recommendation 1", "Recommendation 2"}
    sections, _ = build_chunks("w", "v", ex.text, ex.headings)
    assert [s["section_path"] for s in sections] == ["Recommendation 1", "Recommendation 2"]


def L(text, top, size=12.0, bold=False, x0=54.0):
    """A synthetic PDF line; consecutive body lines are 12pt tall with a 6pt gap."""
    return Line(text=text, top=top, bottom=top + size, x0=x0, size=size, bold=bold)


def body_page(top_text, lines_text, start=100.0):
    """A 792pt page with a running header, body lines, and a page-number footer."""
    lines = [L("Report Title Running Header", 20, size=6)]
    lines += [L(t, start + 18 * k) for k, t in enumerate(lines_text)]
    return (792.0, lines + [L(top_text, 760, size=8)])


def test_layout_paragraphs_headings_and_running_lines():
    page1 = body_page(
        "Page 1 of 3",
        ["first line of para one", "second line of para one."],
    )
    page1[1].insert(1, L("Recommendation 1", 70, size=16))
    page1[1].insert(4, L("Para two starts after a gap.", 160))
    page2 = body_page("Page 2 of 3", ["Bold Heading Line", "Body text under it."])
    page2[1][1].bold = True
    page3 = body_page("Page 3 of 3", ["Closing words."])
    text, page_starts, headings = layout_text([page1, page2, page3])
    assert text.split("\n\n") == [
        "Recommendation 1",
        "first line of para one\nsecond line of para one.",
        "Para two starts after a gap.",
        "Bold Heading Line",
        "Body text under it.",
        "Closing words.",
    ]
    assert headings == {"Recommendation 1", "Bold Heading Line"}
    assert [text[o:].split("\n")[0] for o in page_starts] == [
        "Recommendation 1",
        "Bold Heading Line",
        "Closing words.",
    ]


def test_layout_keeps_sentence_cut_by_page_break_together():
    page1 = (792.0, [L("This sentence runs onto", 700)])
    page2 = (792.0, [L("the next page.", 100), L("New paragraph here.", 140)])
    text, page_starts, _ = layout_text([page1, page2])
    assert text.split("\n\n") == ["This sentence runs onto\nthe next page.", "New paragraph here."]
    assert text[page_starts[1] :].startswith("the next page.")
    _, passages = build_chunks("w", "v", text, page_starts=page_starts, target=3)
    assert (passages[0]["page"], passages[0]["page_end"]) == (1, 2)


def test_layout_wrapped_heading_is_one_heading():
    body = [
        L("Body text that is long enough to set the body font size.", 130 + 18 * k)
        for k in range(2)
    ]
    page = (792.0, [L("A Long Title That", 60, size=24), L("Wraps", 86, size=24), *body[:1]])
    text, _, headings = layout_text([page])
    assert headings == {"A Long Title That Wraps"}
    assert text.split("\n\n") == ["A Long Title That Wraps", body[0].text]


def test_layout_sentence_like_bold_line_is_not_heading():
    page = (792.0, [L("This bold line ends like a sentence.", 100, bold=True), L("Body.", 140)])
    _, _, headings = layout_text([page])
    assert headings == set()


class FakePdfPage:
    def __init__(self, lines, height=792.0):
        self.lines, self.height = lines, height

    def extract_text_lines(self, return_chars=True, strip=True):
        out = []
        for text, top in self.lines:
            chars = [{"text": c, "size": 12.0, "fontname": "ABCDEF+Serif-Regular"} for c in text]
            out.append({"text": text, "top": top, "bottom": top + 12, "x0": 54.0, "chars": chars})
        return out


class FakePdf:
    def __init__(self, pages):
        self.pages = [FakePdfPage(p) for p in pages]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_pdfplumber(monkeypatch, pages):
    mod = types.SimpleNamespace(open=lambda _f: FakePdf(pages))
    monkeypatch.setitem(sys.modules, "pdfplumber", mod)


def test_pdf_page_offsets(monkeypatch):
    p1 = [("A" * 300 + ".", 100), ("more.\r", 140)]
    p2 = [("B" * 300 + ".", 100)]
    fake_pdfplumber(monkeypatch, [p1, p2])
    ex = extract(b"%PDF-1.7")
    assert ex.page_starts[0] == 0
    assert ex.text[ex.page_starts[1] :].startswith("B")
    assert "\r" not in ex.text
    _, passages = build_chunks("w", "v", ex.text, page_starts=ex.page_starts, target=1)
    assert [p["page"] for p in passages] == [1, 1, 2]


def test_scanned_pdf_rejected(monkeypatch):
    fake_pdfplumber(monkeypatch, [[], [("  ", 100)], [("p. 3", 760)]])
    with pytest.raises(Unsupported, match="Textract"):
        extract(b"%PDF-1.7")
