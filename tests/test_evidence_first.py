"""Evidence before verdicts (D98), the blind second opinion (D99), research that must back the
conclusion (D100), and more research behind each suggestion (D101)."""

from pathlib import Path

import pytest

from read import review as R
from read import review_run
from tests.test_review import PARTS, RESEARCH, FakeClient, sent


@pytest.fixture(autouse=True)
def model_id(monkeypatch):
    monkeypatch.setenv("ANSWER_MODEL_ID", "m")


def info(**kw):
    base = {"status": None, "answers": [], "single": True, "cited": [], "explicit": False}
    return base | kw


def verdict(v, mats=("M1",)):
    return {
        "verdicts": [
            {
                "question_id": "doc-alignment",
                "verdict": v,
                "observation": [sent("The lesson practises blending with word cards.", mats)],
                "suggestions": [sent("Tie each activity to the goal.", cites=["S1"])]
                if v != "met"
                else [],
            }
        ]
    }


def problems(v, mats, **kw):
    per_q = {"doc-alignment": info(**kw)}
    texts = {p["id"]: p["text"] for p in PARTS} | RESEARCH
    return R.validate_combine(
        verdict(v, mats), {"doc-alignment"}, per_q, {"M1", "M2", "M3"}, texts, RESEARCH
    )


# ---------- B: judgment verdicts are bound to the evidence gathered before them ----------


def test_judgment_verdict_needs_gathered_support():
    assert any("missing" in p for p in problems("partly", ["M1"], confirmed=[], full=[]))
    ok = problems("partly", ["M1"], confirmed=["M1"], full=[], against=["M2"])
    assert not [p for p in ok if "confirmed" in p or "missing" in p]


def test_against_evidence_rules_out_met_and_support_rules_out_missing():
    against = problems("met", ["M1"], confirmed=["M1"], full=[], against=["M2"])
    assert any("can't be met" in p for p in against)
    assert any(
        "can't be missing" in p for p in problems("missing", [], confirmed=["M1"], full=["M1"])
    )
    assert not [p for p in problems("met", ["M1"], confirmed=["M1"], full=["M1"]) if "met" in p]


def test_question_line_tells_judgment_verdicts_the_graded_evidence():
    q = {"id": "doc-alignment", "question": "Q?", "explicit": False}
    line = R.question_line({**q, "confirmed": ["M1", "M2"], "strong": ["M1"], "against": ["M3"]})
    assert "clearly does this: M1;" in line and "only generally or in part: M2;" in line
    assert "works against this: M3" in line and "met only if a passage clearly does it" in line
    assert "verdict is missing" in R.question_line({**q, "confirmed": [], "against": []})
    assert "separate check" not in R.question_line(q)  # no evidence step ran: no claim made


# ---------- A: the blind reading never sees the verdict ----------


def test_blind_prompt_has_evidence_and_research_but_no_verdict():
    q = {"id": "doc-alignment", "question": "Do the activities fit the goal?", "explicit": False}
    goal = {"material_type": "lesson", "grade_band": "K", "focus": "blending", "objective": "o"}
    text = R.blind_prompt(
        goal,
        q,
        PARTS[:2],
        {"M1": "strong", "M2": "against"},
        [{"cite_id": "S1", "text": RESEARCH["S1"]}],
    )
    assert "M1: clearly does this" in text and "M2: works against this" in text
    assert '<section id="S1">' in text and "Verdict" not in text and "verdict:" not in text.lower()


def test_blind_verdict_strips_ids_and_rejects_bad_output():
    class C:
        def __init__(self, out):
            self.out = out

        def converse(self, **kw):
            return {
                "output": {
                    "message": {
                        "content": [
                            {
                                "toolUse": {
                                    "name": kw["toolConfig"]["toolChoice"]["tool"]["name"],
                                    "input": self.out,
                                }
                            }
                        ]
                    }
                }
            }

    got = R.blind_verdict(
        C({"verdict": "partly", "reason": "M1 practises it (S2) but M3 does not."}), "p"
    )
    assert got["verdict"] == "partly" and "M1" not in got["reason"] and "S2" not in got["reason"]
    assert R.blind_verdict(C({"verdict": "great", "reason": "x"}), "p") is None


class SecondOpinionClient(FakeClient):
    """The independent reading disagrees with check-learning's 'met' and calls it partly."""

    def converse(self, **kw):
        tool = kw["toolConfig"]["toolChoice"]["tool"]["name"]
        if tool == "record_reading":
            self.calls.append(tool)
            text = kw["messages"][0]["content"][0]["text"]
            v = "partly" if "doc-check-learning" in text else "missing"
            out = {"verdict": v, "reason": "The exit ticket checks only part of the skill."}
            return {"output": {"message": {"content": [{"toolUse": {"name": tool, "input": out}}]}}}
        return super().converse(**kw)


def test_disagreement_triggers_a_reconsidered_verdict(monkeypatch):
    monkeypatch.setenv("ANSWER_MODEL_ID", "m")
    goal = {
        "material_type": "lesson plan",
        "grade_band": "K",
        "focus": "blending",
        "objective": "o",
    }

    def search(query, goal):  # as in test_review's end-to-end test: two research sections
        a = "assess" in query or "objective" in query or "align" in query
        sid, txt = ("doc-a:v:s1", RESEARCH["S2"]) if a else ("doc-b:v:s1", RESEARCH["S1"])
        return [{"cite_id": "S1", "text": txt, "label": "Pub, 2016", "ref": {"section_id": sid}}]

    client = SecondOpinionClient()
    res = review_run.run(
        {"parts": PARTS, "sections": R.make_sections(PARTS)},
        goal,
        set(),
        R.load_checklist(Path("rubric/checklist.json")),
        {"embed": lambda t: [1.0, 0.0], "search": search, "client": client},
    )
    rows = {d["question_id"]: d for d in res["document"]}
    learn = rows["doc-check-learning"]["second_opinion"]
    assert learn["first"] == "met" and learn["verdict"] == "partly"
    assert learn["status"] == "uncertain"
    # the fake verdict call can't give 'partly', so the cautious re-decision isn't accepted
    # and the original stands, flagged uncertain (never an unvalidated verdict)
    assert learn["final"] == "met" and rows["doc-check-learning"]["verdict"] == "met"
    assert client.calls.count("record_reading") == 1  # only where evidence was found
    # alignment: the evidence step found nothing, so the verdict was missing by rule, unread
    assert rows["doc-alignment"]["second_opinion"]["status"] == "not needed"
    assert rows["doc-alignment"]["evidence_graded"] == {}


# ---------- C: research behind the conclusion must back the conclusion ----------


def trail(**kw):
    return {
        "evidence": [],
        "gaps": [],
        "research": {"section": "S1", "phrase": "Teach students to blend"},
        "conclusion": "The research calls for daily blending; the lesson does this.",
        "improvements": [],
    } | kw


class Grades:
    def __init__(self, grades):
        self.grades, self.text = grades, ""

    def converse(self, **kw):
        self.text = kw["messages"][0]["content"][0]["text"]
        out = {"items": [{"id": i, "grade": g} for i, g in self.grades.items()]}
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "name": kw["toolConfig"]["toolChoice"]["tool"]["name"],
                                "input": out,
                            }
                        }
                    ]
                }
            }
        }


def test_research_is_graded_against_the_conclusion():
    c = Grades({"R": "no"})
    out = R.research_checks(trail(), RESEARCH, c, "Does it?")
    assert "this conclusion about the material" in c.text and "daily blending" in c.text
    assert out["research"]["dropped"] == "did not back the conclusion" and out["conclusion"] == ""


# ---------- D101: more research behind each suggestion ----------


def improvement(**kw):
    return {
        "section": "S1",
        "phrase": "Teach students to blend",
        "apply": "Add a daily blending routine.",
        "where": "Day 1",
        "also": [{"section": "S2", "phrase": "Check student progress often"}],
    } | kw


def test_extra_sources_are_checked_word_for_word_and_deduplicated():
    out = R.sanitize_trail(
        trail(
            improvements=[
                improvement(
                    also=[
                        {"section": "S2", "phrase": "Check student progress often"},
                        {"section": "S2", "phrase": "duplicate"},
                        {"section": "S1", "phrase": "same as main"},
                    ]
                )
            ]
        ),
        "partly",
        set(),
    )
    assert out["improvements"][0]["also"] == [
        {"section": "S2", "phrase": "Check student progress often"}
    ]
    bad = trail(improvements=[improvement(also=[{"section": "S2", "phrase": "not in the text"}])])
    assert any("also 0" in p for p in R.validate_trail(bad, {}, RESEARCH))
    fixed, notes = R.salvage_trail(bad, {}, RESEARCH)
    assert fixed["improvements"] and fixed["improvements"][0]["also"] == []
    assert any("extra research source" in n for n in notes)


def test_each_extra_source_must_back_the_change_and_can_replace_a_failed_main():
    keep = R.research_checks(
        trail(improvements=[improvement()]),
        RESEARCH,
        Grades({"R": "strong", "I0": "strong", "I0a0": "no"}),
        "Q",
    )
    assert keep["improvements"][0]["also"] == []  # the extra source didn't back it: dropped
    swap = R.research_checks(
        trail(improvements=[improvement()]),
        RESEARCH,
        Grades({"R": "strong", "I0": "no", "I0a0": "limited"}),
        "Q",
    )
    imp = swap["improvements"][0]
    assert imp["section"] == "S2" and imp["match"] == "limited" and imp["also"] == []
    gone = R.research_checks(
        trail(improvements=[improvement(also=[])]),
        RESEARCH,
        Grades({"R": "strong", "I0": "no"}),
        "Q",
    )
    assert gone["improvements"] == []


def test_trail_view_shows_extra_sources_with_highlights():
    view = R.trail_view(trail(improvements=[improvement()]), RESEARCH)
    also = view["improvements"][0]["also"][0]
    assert also["cite_id"] == "S2" and RESEARCH["S2"][also["span"][0] : also["span"][1]].startswith(
        "Check"
    )


class Support:
    """Finds S2 for the suggestion; once with a real quote, once with an invented one."""

    def __init__(self, phrase):
        self.phrase, self.calls = phrase, 0

    def converse(self, **kw):
        self.calls += 1
        tool = kw["toolConfig"]["toolChoice"]["tool"]["name"]
        out = {
            "sources": [{"section": "S2", "phrase": self.phrase}, {"section": "S9", "phrase": "x"}]
        }
        return {"output": {"message": {"content": [{"toolUse": {"name": tool, "input": out}}]}}}


def test_more_support_adds_only_verified_quotes_from_other_sections():
    out = R.more_support(
        trail(improvements=[improvement(also=[])]),
        RESEARCH,
        Support("Check student progress often"),
    )
    assert out["improvements"][0]["also"] == [
        {"section": "S2", "phrase": "Check student progress often"}
    ]
    out = R.more_support(
        trail(improvements=[improvement(also=[])]), RESEARCH, Support("made-up wording")
    )
    assert out["improvements"][0]["also"] == []  # not word for word: not added
    full = Support("Check student progress often")
    R.more_support(
        trail(improvements=[improvement()]), {"S1": RESEARCH["S1"], "S2": RESEARCH["S2"]}, full
    )
    assert full.calls == 0  # every other section is already used: nothing to search
