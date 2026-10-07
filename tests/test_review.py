import io
import json
import os
import time
from pathlib import Path

import pytest

from read import review
from read.extract import extract
from read.store import TempStore

MATERIAL = (
    "Objective: students blend three sounds to read short words.\n\n"
    "Teacher says c-a-t slowly and students say cat. Repeat with map, sit, and hop.\n\n"
    "Exit ticket: each student reads five words aloud."
)
PARTS = review.material_parts(MATERIAL)
RESEARCH = [
    {
        "cite_id": "S1",
        "text": (
            "Teach students to blend individual sounds into words using short daily routines "
            "with feedback."
        ),
    },
    {
        "cite_id": "S2",
        "text": "Check student progress often with short assessments of word reading.",
    },
]
GOAL = {
    "material_type": "lesson plan",
    "grade_band": "K",
    "focus": "blending",
    "objective": "blend CVC words",
}


def good(**kw):
    out = {
        "applies": True,
        "seen": "yes",
        "observation": [
            {
                "text": "The lesson models blending with three short words.",
                "material": ["M2"],
                "cites": [],
            }
        ],
        "suggestions": [
            {
                "text": "Add brief corrective feedback during the blending practice.",
                "material": [],
                "cites": ["S1"],
            }
        ],
    }
    out.update(kw)
    return out


def test_material_parts_are_exact_slices():
    assert [p["id"] for p in PARTS] == ["M1", "M2", "M3"]
    assert all(MATERIAL[p["start"] : p["end"]] == p["text"] for p in PARTS)


def test_valid_review_passes():
    assert review.validate_review(good(), PARTS, RESEARCH) == []


def test_unknown_material_and_research_ids_rejected():
    out = good(observation=[{"text": "x", "material": ["M9"], "cites": ["S7"]}])
    probs = review.validate_review(out, PARTS, RESEARCH)
    assert any("unknown material 'M9'" in p for p in probs)
    assert any("cites unknown 'S7'" in p for p in probs)


def test_suggestion_must_cite_research():
    out = good(suggestions=[{"text": "Do more.", "material": [], "cites": []}])
    assert "suggestions 0 must cite a research section" in review.validate_review(
        out, PARTS, RESEARCH
    )


def test_seen_observation_must_point_at_material():
    out = good(observation=[{"text": "It blends words.", "material": [], "cites": []}])
    assert any(
        "must point to the material" in p for p in review.validate_review(out, PARTS, RESEARCH)
    )


def test_not_seen_needs_no_material_reference():
    out = good(
        seen="no", observation=[{"text": "No progress check appears.", "material": [], "cites": []}]
    )
    assert review.validate_review(out, PARTS, RESEARCH) == []


def test_reusing_material_wording_is_a_quote_not_an_error():
    s = "It says students blend three sounds to read short words, which fits the goal."
    out = good(observation=[{"text": s, "material": ["M1"], "cites": []}])
    assert review.validate_review(out, PARTS, RESEARCH) == []
    shown = review.display(out, PARTS, RESEARCH)["observation"][0]["parts"]
    assert any(p.get("quote") == "students blend three sounds to read short words" for p in shown)


def test_reuse_from_uncited_research_rejected():
    s = "Teach students to blend individual sounds into words using short daily routines."
    out = good(suggestions=[{"text": s, "material": [], "cites": ["S2"]}])
    assert any("without citing it" in p for p in review.validate_review(out, PARTS, RESEARCH))


def test_retry_with_feedback_then_success():
    outs = [good(suggestions=[{"text": "x", "material": [], "cites": []}]), good()]
    feedbacks = []

    def model(c, g, p, e, fb):
        feedbacks.append(fb)
        return outs.pop(0)

    res = review.review_criterion({"question": "q"}, GOAL, PARTS, RESEARCH, model)
    assert res["ok"] and feedbacks[0] is None and "must cite a research section" in feedbacks[1]


def test_applicable_by_grade_band():
    crit = [{"grade_band": "K-1"}, {"grade_band": "2-3"}, {"grade_band": "K-5"}]
    assert review.applicable(crit, "K") == [crit[0], crit[2]]
    assert review.applicable(crit, "3") == [crit[1], crit[2]]
    assert review.applicable(crit, "weird") == crit


def test_clean_goal_normalizes_grade():
    g = review.clean_goal(
        {"material_type": "slides", "grade_band": "Grade 1", "focus": "f", "objective": "o"}
    )
    assert g["grade_band"] == "1"
    assert review.clean_goal({"grade_band": "first"})["grade_band"] == "K-5"


def test_shipped_checklist_is_valid_and_marked_as_placeholder():
    data = review.load_checklist(Path("rubric/checklist.json"))
    assert review.check_checklist(data) == []
    assert data["status"] == "DEVELOPMENT PLACEHOLDER"
    assert "rewritten by Addison" in data["note"]


def test_check_checklist_catches_problems():
    bad = {
        "criteria": [
            {
                "criterion_id": "a",
                "component": "c",
                "grade_band": "Z",
                "question": "q",
                "search_query": "s",
            },
            {
                "criterion_id": "a",
                "component": "",
                "grade_band": "K",
                "question": "q",
                "search_query": "s",
            },
        ]
    }
    probs = review.check_checklist(bad)
    assert any("grade_band" in p for p in probs)
    assert any("duplicate id a" in p for p in probs)
    assert any("component is required" in p for p in probs)


def test_temp_store_purges_after_24_hours(tmp_path):
    store = TempStore(tmp_path, hours=24)
    old, new = store.new(), store.new()
    store.put_json(old, "material.json", {"x": 1})
    past = time.time() - 25 * 3600
    os.utime(tmp_path / old, (past, past))
    assert store.purge() == 1
    assert not (tmp_path / old).exists() and (tmp_path / new).exists()
    with pytest.raises(KeyError):
        store.path(old, "material.json")


def test_pptx_extraction():
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Blending CVC words"
    s.placeholders[1].text = "Model: c-a-t, cat"
    s.notes_slide.notes_text_frame.text = "Keep it short."
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "Check"
    t = s2.shapes.add_table(1, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    t.cell(0, 0).text, t.cell(0, 1).text = "map", "yes"
    buf = io.BytesIO()
    prs.save(buf)
    ex = extract(buf.getvalue())
    assert ex.kind == "pptx"
    assert ex.text.split("\n\n") == [
        "Blending CVC words",
        "Model: c-a-t, cat",
        "Speaker notes: Keep it short.",
        "Check",
        "map | yes",
    ]
    assert ex.headings == {"Blending CVC words", "Check"}
    assert ex.text[ex.page_starts[1] :].startswith("Check")
    json.dumps(ex.page_starts)
