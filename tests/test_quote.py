from read.answer import validate
from read.quote import copied_spans, display_quote, quote_parts
from read.service import sentence_parts

SECTION = (
    "Recommendation 1. Teach students academic language skills, including the use of\n"
    "inferential and narrative language, and vocabulary knowledge. Academic language struc-\n"
    "tures are common in books."
)
OTHER = "Teachers should model fluent reading aloud every day with expression and accuracy."
EV = [
    {"cite_id": "S1", "text": SECTION, "section": {"section_id": "w:v:s0", "work_id": "w"}},
    {"cite_id": "S2", "text": OTHER, "section": {"section_id": "w:v:s1", "work_id": "w"}},
]
TEXTS = {e["cite_id"]: e["text"] for e in EV}
REUSED = "teach students academic language skills including the use of inferential"
SOURCE_WORDS = "Teach students academic language skills, including the use of inferential"


def answer(text, cites=("S1",)):
    return {
        "answerable": True,
        "evidence_strength": "limited",
        "strength_reason": "r",
        "sentences": [{"text": text, "cites": list(cites)}],
    }


def test_copied_spans_ignore_case_punctuation_and_line_breaks():
    s = f"The guide says: {REUSED} language."
    (sp,) = copied_spans(s, SECTION)
    assert sp.words == 10
    assert s[sp.sent_start : sp.sent_end] == REUSED
    assert display_quote(SECTION[sp.sec_start : sp.sec_end]) == SOURCE_WORDS


def test_short_overlap_is_not_a_quote():
    assert copied_spans("Teach students academic language skills daily.", SECTION) == []


def test_reuse_from_a_cited_section_is_allowed():
    assert validate(answer(f"First, {REUSED} and narrative language."), EV) == []


def test_reuse_from_an_uncited_section_is_rejected():
    text = "Teachers should model fluent reading aloud every day with expression in class."
    probs = validate(answer(text, cites=("S1",)), EV)
    assert probs and "without citing it" in probs[0]


def test_reuse_over_the_length_limit_is_rejected():
    long = " ".join(f"w{k}" for k in range(45))
    ev = [{"cite_id": "S1", "text": long, "section": {"section_id": "w:v:s0", "work_id": "w"}}]
    probs = validate(answer(long + "."), ev)
    assert probs and "reuses 45 consecutive words" in probs[0]


def test_parts_show_source_words_with_source_punctuation():
    s = f"The first step is to {REUSED} language daily."
    assert quote_parts(s, ["S1"], TEXTS) == [
        {"text": "The first step is to "},
        {"quote": SOURCE_WORDS, "cite": "S1"},
        {"text": " language daily."},
    ]


def test_models_own_quotation_marks_are_not_doubled():
    parts = quote_parts(f'It says "{REUSED}" here.', ["S1"], TEXTS)
    assert parts == [
        {"text": "It says "},
        {"quote": SOURCE_WORDS, "cite": "S1"},
        {"text": " here."},
    ]


def test_quote_only_from_cited_sections():
    s = f"The first step is to {REUSED} language daily."
    assert quote_parts(s, ["S2"], TEXTS) == [{"text": s}]


def test_sentence_without_reuse_is_one_text_part():
    parts = sentence_parts({"text": "Read aloud daily.", "cites": ["S1"]}, EV)["parts"]
    assert parts == [{"text": "Read aloud daily."}]


def test_display_quote_never_drops_a_source_character():
    assert display_quote("Academic language struc-\ntures are\n common") == (
        "Academic language struc-tures are common"
    )
    assert display_quote("an instructional-\nlevel text") == "an instructional-level text"
