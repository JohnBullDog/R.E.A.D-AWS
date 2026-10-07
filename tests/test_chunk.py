import pytest

from read.chunk import ChunkError, build_chunks, page_at, paragraphs, sha, split_long, verify


def check_offsets(text, sections, passages):
    for p in passages:
        assert text[p["char_start"] : p["char_end"]] == p["text"]
        assert sha(p["text"]) == p["text_sha256"]
    for s in sections:
        assert sha(text[s["char_start"] : s["char_end"]]) == s["text_sha256"]


def test_paragraph_offsets_are_trimmed_and_exact():
    text = "  First para\nstill first.\n\n\n   Second.  \n \nThird"
    spans = list(paragraphs(text))
    assert [text[s:e] for s, e in spans] == ["First para\nstill first.", "Second.", "Third"]


def test_unicode_offsets_reproduce_text():
    text = "Chapter 1\n\nPhonemic awareness — “sounds” in words. Ñandú 🦜 café.\n\n" + (
        "Ünïcödé wörds ✓ 漢字 かな. " * 60
    )
    sections, passages = build_chunks("w", "v1", text)
    check_offsets(text, sections, passages)
    assert sections[0]["section_path"] == "Chapter 1"


def test_no_headings_caps_sections_at_500_words():
    para = " ".join(["word"] * 101) + "."  # 101 words
    text = "\n\n".join([para] * 40)
    sections, passages = build_chunks("w", "v1", text)
    check_offsets(text, sections, passages)
    assert len(sections) == 10  # 4 paragraphs each; a 5th would pass 500 words
    assert all(s["section_path"] is None for s in sections)
    assert all(len(p["text"].split()) <= 250 for p in passages)


def test_no_headings_caps_sections_at_5_paragraphs():
    text = "\n\n".join(f"Short paragraph {i}." for i in range(12))
    sections, _ = build_chunks("w", "v1", text)
    sizes = [text[s["char_start"] : s["char_end"]].count("\n\n") + 1 for s in sections]
    assert sizes == [5, 5, 2]


def test_long_headed_section_continues_under_same_heading():
    body = "\n\n".join([" ".join(["word"] * 300) + "."] * 6)  # 1806 words
    text = "Recommendation 1\n\n" + body + "\n\nRecommendation 2\n\nEnd."
    sections, _ = build_chunks("w", "v1", text)
    assert [s["section_path"] for s in sections] == [
        "Recommendation 1",
        "Recommendation 1",
        "Recommendation 2",
    ]


def test_sentence_ending_line_is_not_a_heading():
    text = "Step 2 is to blend sounds.\n\nMore text."
    sections, _ = build_chunks("w", "v1", text)
    assert sections[0]["section_path"] is None


def test_very_long_paragraph_is_split_at_sentences():
    sentence = " ".join(["alpha"] * 29) + " end."  # 30 words
    text = " ".join([sentence] * 40)  # one 1200-word paragraph
    sections, passages = build_chunks("w", "v1", text)
    check_offsets(text, sections, passages)
    assert len(passages) > 1
    for p in passages:
        assert p["text"].endswith("end.")
        assert len(p["text"].split()) <= 400


def test_sentence_longer_than_limit_is_split_at_words():
    text = " ".join(f"w{i}" for i in range(1000))  # no sentence ends at all
    pieces = list(split_long(text, 0, len(text), 400))
    assert [len(text[s:e].split()) for s, e in pieces] == [400, 400, 200]
    assert " ".join(text[s:e] for s, e in pieces) == text


def test_split_long_handles_closing_quotes():
    text = " ".join(['He said "stop."'] + ["x"] * 5 + ['She said "go!"'] + ["y"] * 5)
    pieces = [text[s:e] for s, e in split_long(text, 0, len(text), 10)]
    assert pieces[0] == 'He said "stop."'
    assert pieces[1] == 'x x x x x She said "go!"'
    assert pieces[2] == "y y y y y"


def test_passages_never_cross_sections():
    body = " ".join(["text"] * 200)
    text = "\n\n".join(["Chapter 1", body, body, "Chapter 2", body, "Section 3.1", body])
    sections, passages = build_chunks("w", "v1", text)
    check_offsets(text, sections, passages)
    assert [s["section_path"] for s in sections] == ["Chapter 1", "Chapter 2", "Section 3.1"]
    by_id = {s["section_id"]: s for s in sections}
    for p in passages:
        s = by_id[p["section_id"]]
        assert s["char_start"] <= p["char_start"] < p["char_end"] <= s["char_end"]


def test_explicit_headings_from_extractor():
    text = "Recommendation 1\n\nTeach students to blend sounds.\n\nRecommendation 2\n\nRead daily."
    heads = {"Recommendation 1", "Recommendation 2"}
    sections, _ = build_chunks("w", "v1", text, headings=heads, min_sec=0)
    assert [s["section_path"] for s in sections] == ["Recommendation 1", "Recommendation 2"]


def test_ids_format():
    sections, passages = build_chunks("wwc-2016", "abc", "Chapter 1\n\nHello.")
    assert sections[0]["section_id"] == "wwc-2016:abc:s0000"
    assert passages[0]["chunk_id"] == "wwc-2016:abc:p00000"
    assert passages[0]["section_id"] == "wwc-2016:abc:s0000"


def test_pages_from_page_starts():
    page1 = "Page one text.\n\n"
    text = page1 + "Page two text.\n\n"
    sections, passages = build_chunks("w", "v1", text, page_starts=[0, len(page1)], target=2)
    assert [p["page"] for p in passages] == [1, 2]
    assert page_at([], 5) is None
    sections, _ = build_chunks("w", "v1", text, page_starts=[0, len(page1)])
    assert (sections[0]["page"], sections[0]["page_end"]) == (1, 2)


def test_empty_text():
    assert build_chunks("w", "v1", "") == ([], [])
    assert build_chunks("w", "v1", "\n\n  \n") == ([], [])


def test_verify_rejects_tampered_passage():
    text = "Chapter 1\n\nSome words here."
    sections, passages = build_chunks("w", "v1", text)
    passages[0]["text"] = passages[0]["text"].replace("Some", "Same")
    with pytest.raises(ChunkError):
        verify(text, sections, passages)


def test_small_sections_merge_until_40_words():
    body = " ".join(["word"] * 50) + "."
    text = "\n\n".join(["Recommendation 1", "Teach academic language", body, "Step 1", body])
    heads = {"Recommendation 1", "Teach academic language", "Step 1"}
    sections, _ = build_chunks("w", "v1", text, headings=heads)
    assert [s["section_path"] for s in sections] == ["Recommendation 1", "Step 1"]
    first = text[sections[0]["char_start"] : sections[0]["char_end"]]
    assert first.startswith("Recommendation 1\n\nTeach academic language\n\n")
