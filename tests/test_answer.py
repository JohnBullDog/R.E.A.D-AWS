import pytest

from read.answer import SCHEMA, TOOL_NAME, call_model, cap_strength, cited_answer, ngrams, validate

SRC1 = (
    "Explicit instruction in phonemic awareness helps kindergarten students learn to "
    "segment and blend the individual sounds in spoken words before they read."
)
SRC2 = "Daily practice reading connected text builds fluency and supports comprehension."


def ev(cite_id, text, work_id, **sec):
    return {
        "cite_id": cite_id,
        "text": text,
        "section": {"section_id": f"{work_id}:v:s0", "work_id": work_id, **sec},
    }


EVIDENCE = [ev("S1", SRC1, "wwc"), ev("S2", SRC2, "nrp")]
WORKS = {
    "wwc": {"work_id": "wwc", "doc_type": "practice_guide"},
    "nrp": {"work_id": "nrp", "doc_type": "systematic_review"},
    "fcrr": {"work_id": "fcrr", "doc_type": "practitioner_resource"},
    "ncil": {"work_id": "ncil", "doc_type": "practitioner_resource"},
}


def good(**kw):
    out = {
        "answerable": True,
        "evidence_strength": "strong",
        "strength_reason": "Two sources agree.",
        "sentences": [
            {"text": "Teaching sound awareness directly helps young readers.", "cites": ["S1"]},
            {"text": "Regular practice with real text improves reading speed.", "cites": ["S2"]},
        ],
    }
    out.update(kw)
    return out


def test_valid_answer_passes():
    assert validate(good(), EVIDENCE) == []


def test_unknown_cite():
    out = good(sentences=[{"text": "Something true.", "cites": ["S9"]}])
    assert validate(out, EVIDENCE) == ["sentence 0 cites unknown 'S9'"]


def test_malformed_cite_rejected_even_if_schema_bypassed():
    out = good(sentences=[{"text": "Something true.", "cites": ["IES 2016, p. 4"]}])
    assert "cites unknown" in validate(out, EVIDENCE)[0]


def test_missing_cite_when_answerable():
    out = good(sentences=[{"text": "Something true.", "cites": []}])
    assert validate(out, EVIDENCE) == ["sentence 0 has no citation"]


def test_reused_wording_from_cited_section_is_allowed_as_a_quote():
    copied = "It helps Kindergarten students learn to segment, and blend the individual sounds!"
    out = good(sentences=[{"text": copied, "cites": ["S1"]}])
    assert validate(out, EVIDENCE) == []


def test_reused_wording_from_uncited_section_is_rejected():
    copied = "It helps Kindergarten students learn to segment, and blend the individual sounds!"
    out = good(sentences=[{"text": copied, "cites": ["S2"]}])
    assert "reuses wording from S1 without citing it" in validate(out, EVIDENCE)[0]


def test_seven_words_shared_is_allowed():
    out = good(
        sentences=[
            {
                "text": "Kindergarten students learn to segment and blend, research says.",
                "cites": ["S1"],
            }
        ]
    )
    assert validate(out, EVIDENCE) == []


def test_unanswerable_path_allows_uncited_explanation():
    out = good(
        answerable=False,
        evidence_strength="limited",
        sentences=[{"text": "The sections do not address spelling.", "cites": []}],
    )
    assert validate(out, EVIDENCE) == []


def test_bad_shape_reported_not_raised():
    assert validate({"answerable": "yes", "sentences": "x"}, EVIDENCE)
    assert validate("nope", EVIDENCE) == ["output is not an object"]
    assert "no sentences" in validate(good(sentences=[]), EVIDENCE)


def test_ngrams_basic():
    assert len(ngrams("one two three four five six seven eight nine")) == 2
    assert ngrams("too short") == set()


def test_cap_single_source_to_limited():
    out = good(sentences=[{"text": "A paraphrase.", "cites": ["S1"]}])
    flag, reasons = cap_strength(out, EVIDENCE, WORKS)
    assert flag == "limited" and "fewer than two distinct sources cited" in reasons


def test_cap_practitioner_only_to_limited():
    evidence = [ev("S1", "a", "fcrr"), ev("S2", "b", "ncil")]
    out = good(sentences=[{"text": "x", "cites": ["S1", "S2"]}])
    assert cap_strength(out, evidence, WORKS) == ("limited", ["only practitioner resources cited"])


def test_cap_wwc_minimal_to_limited():
    evidence = [ev("S1", "a", "wwc", wwc_evidence_level="minimal"), ev("S2", "b", "nrp")]
    flag, reasons = cap_strength(good(), evidence, WORKS)
    assert flag == "limited"


def test_strong_kept_when_checks_pass():
    assert cap_strength(good(), EVIDENCE, WORKS) == ("strong", [])


def test_retry_once_then_error_state():
    calls = []

    def model(q, e, fb):
        calls.append(1)
        return good(sentences=[{"text": "x", "cites": ["S7"]}])

    res = cited_answer("q", EVIDENCE, WORKS, model)
    assert len(calls) == 2
    assert res["ok"] is False and res["error"] == "answer_unavailable"


def test_retry_succeeds_on_second_attempt():
    outs = [good(sentences=[{"text": "x", "cites": []}]), good()]
    res = cited_answer("q", EVIDENCE, WORKS, lambda q, e, fb: outs.pop(0))
    assert res["ok"] is True and res["evidence_strength"] == "strong"


class FakeBedrock:
    def __init__(self, content):
        self.content, self.kwargs = content, None

    def converse(self, **kwargs):
        self.kwargs = kwargs
        return {"output": {"message": {"content": self.content}}}


def test_call_model_forces_tool_and_reads_model_id_from_env(monkeypatch):
    monkeypatch.setenv("ANSWER_MODEL_ID", "test-model")
    fake = FakeBedrock([{"toolUse": {"name": TOOL_NAME, "input": good()}}])
    assert call_model("q", EVIDENCE, fake) == good()
    kw = fake.kwargs
    assert kw["modelId"] == "test-model"
    assert kw["inferenceConfig"]["temperature"] == 0
    assert kw["toolConfig"]["toolChoice"] == {"tool": {"name": TOOL_NAME}}
    assert kw["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"] is SCHEMA
    prompt = kw["messages"][0]["content"][0]["text"]
    assert '<section id="S1">' in prompt and "wwc" not in prompt  # no source identity in the prompt


def test_call_model_without_tool_use_raises(monkeypatch):
    monkeypatch.setenv("ANSWER_MODEL_ID", "m")
    with pytest.raises(ValueError):
        call_model("q", EVIDENCE, FakeBedrock([{"text": "free text"}]))


def test_decline_with_no_sentences_is_valid():
    out = good(answerable=False, evidence_strength="limited", sentences=[])
    assert validate(out, EVIDENCE) == []


def test_decline_still_rejects_bad_cites():
    out = good(answerable=False, sentences=[{"text": "Not covered.", "cites": ["S9"]}])
    assert validate(out, EVIDENCE) == ["sentence 0 cites unknown 'S9'"]


def test_system_prompt_states_the_reuse_limit():
    from read.answer import MAX_QUOTE_WORDS, SYSTEM

    assert f"Never reuse more than {MAX_QUOTE_WORDS} consecutive words" in SYSTEM


def test_retry_tells_the_model_what_it_copied():
    copied = "It helps Kindergarten students learn to segment, and blend the individual sounds!"
    outs = [good(sentences=[{"text": copied, "cites": ["S2"]}]), good()]
    feedbacks = []

    def model(q, e, fb):
        feedbacks.append(fb)
        return outs.pop(0)

    res = cited_answer("q", EVIDENCE, WORKS, model)
    assert res["ok"] and feedbacks[0] is None
    assert "reuses wording from S1 without citing it" in feedbacks[1]
    assert "Kindergarten students learn to segment, and blend the individual sounds" in feedbacks[1]
    assert copied in feedbacks[1]  # the rejected sentence is shown back
    assert [len(a["problems"]) for a in res["attempts"]] == [1, 0]


def test_call_model_appends_feedback(monkeypatch):
    monkeypatch.setenv("ANSWER_MODEL_ID", "m")
    fake = FakeBedrock([{"toolUse": {"name": TOOL_NAME, "input": good()}}])
    call_model("q", EVIDENCE, fake, "FIX THIS")
    assert fake.kwargs["messages"][0]["content"][0]["text"].endswith("\n\nFIX THIS")
