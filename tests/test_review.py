import io
import os
import time
from pathlib import Path

import pytest

from read import review as R
from read import review_run
from read.extract import extract
from read.store import TempStore

SHORT = (
    "Objective: students blend three sounds to read short words.\n\n"
    "Teacher says c-a-t slowly and students say cat. Repeat with map, sit, and hop.\n\n"
    "Exit ticket: each student reads five words aloud."
)
PARTS = R.material_parts(SHORT)
SECTION = {
    "id": "sec1",
    "title": "Whole material",
    "parts": ["M1", "M2", "M3"],
    "owned": ["M1", "M2", "M3"],
}
RESEARCH = {
    "S1": (
        "Teach students to blend individual sounds into words using short daily routines "
        "with feedback."
    ),
    "S2": "Check student progress often with short assessments of word reading.",
}
TEXTS = {p["id"]: p["text"] for p in PARTS} | RESEARCH


def sent(text, material=(), cites=()):
    return {"text": text, "material": list(material), "cites": list(cites)}


def section_out(**kw):
    out = {
        "findings": [
            {
                "question_id": "phonological-awareness",
                "seen": "yes",
                "observation": [sent("The lesson models blending with three short words.", ["M2"])],
                "suggestions": [
                    sent("Add brief corrective feedback during blending.", cites=["S1"])
                ],
            }
        ],
        "doc": [
            {
                "question_id": "doc-check-learning",
                "answer": "full",
                "material": ["M3"],
                "note": "exit ticket",
            }
        ],
    }
    out.update(kw)
    return out


def validate(out):
    return R.validate_section(
        out, SECTION, {"phonological-awareness"}, {"doc-check-learning"}, TEXTS, RESEARCH
    )


# ---------------- checklist ----------------


def test_shipped_checklist_is_valid_two_lists_and_placeholder():
    data = R.load_checklist(Path("rubric/checklist.json"))
    assert R.check_checklist(data) == []
    assert data["status"] == "DEVELOPMENT PLACEHOLDER" and "rewritten by Addison" in data["note"]
    a, b = R.split_checklist(data["criteria"], "K-12")
    assert len(a) == 7 and len(b) == 7
    assert {q["criterion_id"] for q in b if q["core"]} == {
        "doc-objective",
        "doc-alignment",
        "doc-check-learning",
    }


def test_split_checklist_by_grade():
    data = R.load_checklist(Path("rubric/checklist.json"))
    a, _ = R.split_checklist(data["criteria"], "4")
    assert "phonological-awareness" not in {q["criterion_id"] for q in a}  # K-1 only


def test_check_checklist_catches_scope_and_combine():
    bad = {
        "criteria": [
            {
                "criterion_id": "x",
                "component": "c",
                "grade_band": "K",
                "question": "q",
                "search_query": "s",
                "scope": "document",
                "combine": "sometimes",
            },
            {
                "criterion_id": "y",
                "component": "c",
                "grade_band": "K",
                "question": "q",
                "search_query": "s",
                "scope": "page",
            },
        ]
    }
    probs = R.check_checklist(bad)
    assert any("combine must be one of" in p for p in probs)
    assert any("scope must be section or document" in p for p in probs)


# ---------------- sections ----------------


def para(n, word="word"):
    return " ".join([word] * (n - 1)) + " end."


def test_short_material_is_one_section():
    assert R.make_sections(PARTS) == [
        {
            "id": "sec1",
            "title": "Whole material",
            "parts": ["M1", "M2", "M3"],
            "owned": ["M1", "M2", "M3"],
        }
    ]


def test_headed_material_splits_at_headings_caps_and_merges_tiny_groups():
    text = "\n\n".join(
        [
            "Unit plan",
            "Day 1: Blending",
            para(400),
            para(400),
            para(300),
            "Day 2: Segmenting",
            para(500),
            "Wrap-up",
            para(200),
        ]
    )
    parts = R.material_parts(text)
    secs = R.make_sections(parts)
    titles = [s["title"] for s in secs]
    assert titles[0] == "Day 1: Blending"  # a lone heading merges into the next group
    assert "Day 1: Blending (cont.)" in titles  # 1,100 words capped at 800
    assert titles[-2:] == ["Day 2: Segmenting", "Wrap-up"]
    assert all(s["owned"] == s["parts"] for s in secs)
    flat = [m for s in secs for m in s["parts"]]
    assert flat == [p["id"] for p in parts]  # every part once, in order


def test_unstructured_material_uses_sliding_windows_with_overlap():
    text = "\n\n".join(para(80) for _ in range(30))  # 2,400 words, no headings
    parts = R.material_parts(text)
    secs = R.make_sections(parts)
    assert len(secs) > 1 and secs[0]["title"] == "Part 1"
    second = secs[1]
    overlap = [m for m in second["parts"] if m not in second["owned"]]
    assert overlap and set(overlap) <= set(secs[0]["parts"])
    owned = [m for s in secs for m in s["owned"]]
    assert owned == [p["id"] for p in parts]  # each part owned exactly once


def test_dedupe_overlap_drops_findings_only_about_overlap():
    sec = {"parts": ["M1", "M2", "M3"], "owned": ["M2", "M3"]}
    out = {
        "findings": [
            {"observation": [sent("a", ["M1"])]},
            {"observation": [sent("b", ["M1", "M2"])]},
        ],
        "doc": [],
    }
    assert len(R.dedupe_overlap(out, sec)["findings"]) == 1


# ---------------- triage and goal ----------------


def test_triage_thresholds():
    unit = [1.0, 0.0]
    t = R.triage(unit, {"a": [0.9, 0.436], "b": [0.3, 0.954], "c": [0.1, 0.995]})
    assert (t["a"]["label"], t["b"]["label"], t["c"]["label"]) == ("in", "maybe", "out")


def test_clean_goal_forces_core_and_keeps_reasons():
    qs = [
        {"criterion_id": "doc-objective", "question": "q1", "core": True},
        {"criterion_id": "doc-support", "question": "q2", "core": False},
        {"criterion_id": "doc-sequence", "question": "q3", "core": False},
    ]
    g = R.clean_goal(
        {
            "grade_band": "Grade 1",
            "objective": "o",
            "doc_questions": [
                {"id": "doc-objective", "relevant": False, "reason": "x"},
                {"id": "doc-support", "relevant": True, "reason": "has groups"},
            ],
        },
        qs,
    )
    assert g["grade_band"] == "1"
    rel = {d["id"]: d["relevant"] for d in g["doc_questions"]}
    assert rel == {"doc-objective": True, "doc-support": True, "doc-sequence": False}
    assert g["doc_questions"][1]["reason"] == "has groups"


# ---------------- section validation ----------------


def test_valid_section_passes():
    assert validate(section_out()) == []


def test_section_rejects_unknown_ids_and_outside_material():
    out = section_out(
        findings=[{"question_id": "nope", "seen": "yes", "observation": [], "suggestions": []}],
        doc=[
            {"question_id": "doc-check-learning", "answer": "full", "material": ["M9"], "note": ""}
        ],
    )
    probs = validate(out)
    assert any("must use one of the section question ids" in p for p in probs)
    assert any("outside this section" in p for p in probs)


def test_section_requires_every_doc_answer_and_evidence():
    probs = validate(section_out(doc=[]))
    assert any("missing: doc-check-learning" in p for p in probs)
    probs = validate(
        section_out(
            doc=[
                {
                    "question_id": "doc-check-learning",
                    "answer": "partial",
                    "material": [],
                    "note": "",
                }
            ]
        )
    )
    assert any("must point to the material parts" in p for p in probs)


def test_section_finding_rules():
    f = section_out()["findings"][0]
    probs = validate(section_out(findings=[{**f, "observation": [sent("It blends.")]}]))
    assert any("must point to the material parts" in p for p in probs)
    probs = validate(section_out(findings=[{**f, "suggestions": [sent("Do more.")]}]))
    assert any("must cite a research section" in p for p in probs)


def test_reused_material_wording_is_a_quote():
    s = sent(
        "It says students blend three sounds to read short words, which fits the goal.", ["M1"]
    )
    out = section_out(findings=[{**section_out()["findings"][0], "observation": [s]}])
    assert validate(out) == []
    parts = R.show([s], TEXTS)[0]["parts"]
    assert any(p.get("quote") == "students blend three sounds to read short words" for p in parts)


# ---------------- combine and summary ----------------


def test_combine_status_rules():
    assert R.combine_status("any", ["none", "partial", "none"]) == "partly"
    assert R.combine_status("any", ["none", "full"]) == "met"
    assert R.combine_status("any", ["none", "none"]) == "missing"
    assert R.combine_status("all", ["full", "full"]) == "met"
    assert R.combine_status("all", ["full", "none"]) == "partly"
    assert R.combine_status("all", ["none"]) == "missing"
    assert R.combine_status("judge", ["full"]) is None


def test_validate_combine_enforces_fixed_and_missing_rules():
    per = {
        "a": {"status": "met", "answers": [{"answer": "full"}]},
        "b": {"status": None, "answers": [{"answer": "partial"}]},
    }
    out = {
        "verdicts": [
            {
                "question_id": "a",
                "verdict": "partly",
                "observation": [sent("x", ["M1"])],
                "suggestions": [sent("y", cites=["S1"])],
            },
            {
                "question_id": "b",
                "verdict": "missing",
                "observation": [sent("x")],
                "suggestions": [],
            },
        ]
    }
    probs = R.validate_combine(out, {"a", "b"}, per, {"M1", "M2", "M3"}, TEXTS, RESEARCH)
    assert any("must be met" in p for p in probs)
    assert any("can't be missing" in p for p in probs)


def test_validate_summary():
    good = {
        "takeaways": [{"text": f"Point {i}.", "refs": ["F1"], "cites": ["S1"]} for i in range(3)]
    }
    assert R.validate_summary(good, {"F1", "D1"}, RESEARCH) == []
    probs = R.validate_summary(
        {"takeaways": [{"text": "x", "refs": ["F9"], "cites": []}]}, {"F1"}, RESEARCH
    )
    assert any("3 to 6" in p for p in probs)
    assert any("unknown 'F9'" in p for p in probs)
    assert any("must cite a research section" in p for p in probs)


# ---------------- end to end with fakes ----------------


class FakeClient:
    def __init__(self):
        self.calls = []

    def converse(self, **kw):
        tool = kw["toolConfig"]["toolChoice"]["tool"]["name"]
        self.calls.append(tool)
        text = kw["messages"][0]["content"][0]["text"]
        if tool == "record_section":
            out = {
                "findings": [
                    {
                        "question_id": "phonological-awareness",
                        "seen": "yes",
                        "observation": [sent("The lesson models blending.", ["M2"])],
                        "suggestions": [sent("Give quick feedback while blending.", cites=["S1"])],
                    }
                ],
                "doc": [
                    {
                        "question_id": q,
                        "answer": "full" if q == "doc-check-learning" else "none",
                        "material": ["M3"] if q == "doc-check-learning" else [],
                        "note": "",
                    }
                    for q in ("doc-objective", "doc-alignment", "doc-check-learning")
                    if f"- {q}:" in text
                ],
            }
        elif tool == "record_verdicts":
            out = {
                "verdicts": [
                    {
                        "question_id": "doc-objective",
                        "verdict": "missing",
                        "observation": [sent("No objective is stated.")],
                        "suggestions": [sent("State the goal at the start.", cites=["S2"])],
                    },
                    {
                        "question_id": "doc-alignment",
                        "verdict": "missing",
                        "observation": [sent("Fit is unclear.")],
                        "suggestions": [sent("Tie each activity to the goal.", cites=["S2"])],
                    },
                    {
                        "question_id": "doc-check-learning",
                        "verdict": "met",
                        "observation": [sent("An exit ticket checks.", ["M3"])],
                        "suggestions": [sent("Keep the exit ticket.", cites=["S2"])],
                    },
                ]
            }
            asked = [v for v in out["verdicts"] if f"{v['question_id']} (" in text]
            out = {"verdicts": asked}  # one call per question
        else:
            out = {
                "takeaways": [
                    {"text": f"Takeaway {i}.", "refs": ["F1", "D1"], "cites": ["S1"]}
                    for i in range(3)
                ]
            }
        return {"output": {"message": {"content": [{"toolUse": {"name": tool, "input": out}}]}}}


def test_run_end_to_end_with_fakes(monkeypatch):
    monkeypatch.setenv("ANSWER_MODEL_ID", "m")
    checklist = R.load_checklist(Path("rubric/checklist.json"))
    material = {"parts": PARTS, "sections": R.make_sections(PARTS)}
    goal = {
        "material_type": "lesson plan",
        "grade_band": "K",
        "focus": "blending",
        "objective": "blend CVC words",
    }

    def embed(text):  # every question looks relevant to every section
        return [1.0, 0.0]

    def search(query, goal):
        sid = (
            "doc-a:v:s1"
            if "assess" in query or "objective" in query or "align" in query
            else "doc-b:v:s1"
        )
        txt = RESEARCH["S2"] if sid == "doc-a:v:s1" else RESEARCH["S1"]
        return [
            {"cite_id": "S1", "text": txt, "label": "Pub, 2016, p. 1", "ref": {"section_id": sid}}
        ]

    client = FakeClient()
    res = review_run.run(
        material, goal, set(), checklist, {"embed": embed, "search": search, "client": client}
    )
    assert client.calls.count("record_section") == 1  # short material = one section
    assert res["sections"][0]["findings"][0]["id"] == "F1"
    assert [d["question_id"] for d in res["document"]] == [
        "doc-objective",
        "doc-alignment",
        "doc-check-learning",
    ]
    assert [d["status"] for d in res["document"]] == ["missing", "missing", "met"]
    assert res["document_ok"] and res["summary"]["ok"] and len(res["summary"]["takeaways"]) == 3
    assert set(res["skipped_document_questions"]) == {
        "doc-sequence",
        "doc-support",
        "doc-practice-time",
        "doc-ms-standards",
    }
    assert set(res["research"]) == {"S1", "S2"}


# ---------------- temp store and PowerPoint ----------------


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


def test_heading_on_the_line_above_a_paragraph_starts_a_section():
    body = "\n\n".join(f"Day {d}: Practice\n" + para(420) for d in range(1, 5))
    parts = R.material_parts(body)
    assert all(body[p["start"] : p["end"]] == p["text"] for p in parts)
    assert parts[0]["text"] == "Day 1: Practice"
    assert [s["title"] for s in R.make_sections(parts)] == [
        f"Day {d}: Practice" for d in range(1, 5)
    ]


def test_with_retry_survives_a_bedrock_error():
    calls = []

    def call(fb):
        calls.append(fb)
        if len(calls) == 1:
            raise RuntimeError("ModelErrorException: invalid sequence as part of ToolUse")
        return {"ok": 1}

    res = R.with_retry(call, lambda o: [])
    assert res["ok"] and len(calls) == 2 and "invalid sequence" in calls[1]


def test_sanitize_section_fixes_what_is_safe():
    reused = "Teach students to blend individual sounds into words using short daily routines."
    raw = {
        "findings": [
            {
                "question_id": "made-up",
                "seen": "yes",
                "observation": [sent("x", ["M1"])],
                "suggestions": [],
            },
            {
                "question_id": "phonological-awareness",
                "seen": "no",
                "observation": [],
                "suggestions": [],
            },
            {
                "question_id": "phonological-awareness",
                "seen": "yes",
                "observation": [sent("The lesson models blending.", ["M2"])],
                "suggestions": [sent(reused)],
            },
        ],
    }
    clean, notes = R.sanitize_section(
        raw, {"phonological-awareness"}, {"doc-check-learning"}, SECTION, RESEARCH, TEXTS
    )
    assert len(clean["findings"]) == 1
    assert clean["findings"][0]["suggestions"][0]["cites"] == ["S1"]  # reuse gets its citation
    assert clean["doc"] == [
        {
            "question_id": "doc-check-learning",
            "answer": "unanswered",
            "material": [],
            "note": "not answered",
        }
    ]
    assert validate(clean) == []
    assert len(notes) == 3


def test_combine_must_cover_every_question():
    per = {
        "a": {"status": "met", "answers": [{"answer": "full"}]},
        "b": {"status": "missing", "answers": []},
    }
    out = {
        "verdicts": [
            {
                "question_id": "a",
                "verdict": "met",
                "observation": [sent("ok", ["M1"])],
                "suggestions": [sent("keep it", cites=["S1"])],
            }
        ]
    }
    probs = R.validate_combine(out, {"a", "b"}, per, {"M1"}, TEXTS, RESEARCH)
    assert probs == ["write an entry for every question, including fixed verdicts; missing: b"]


def test_unanswered_sections_are_left_out_of_the_combine():
    assert R.combine_status("all", ["full", "unanswered", "full"]) == "met"
    assert R.combine_status("any", ["unanswered"]) == "missing"


def test_borderline_not_seen_finding_is_dropped():
    f = {
        "question_id": "phonological-awareness",
        "seen": "no",
        "observation": [sent("No sound work here.")],
        "suggestions": [],
    }
    for label, kept in (("maybe", 0), ("in", 1)):
        clean, _ = R.sanitize_section(
            {"findings": [dict(f)], "doc": []},
            {"phonological-awareness"},
            set(),
            SECTION,
            RESEARCH,
            TEXTS,
            {"phonological-awareness": label},
        )
        assert len(clean["findings"]) == kept


def test_summary_rejects_ids_written_in_text():
    bad = {
        "takeaways": [
            {"text": f"Point {i}, see F1 and S1.", "refs": ["F1"], "cites": ["S1"]}
            for i in range(3)
        ]
    }
    probs = R.validate_summary(bad, {"F1"}, RESEARCH)
    assert len([p for p in probs if "writes IDs in its text" in p]) == 3


def test_strip_ids_removes_citation_talk_only():
    t = (
        "Students practice blending daily. This is supported by findings F1, F2, and F5 "
        "and research sections S3 and S12."
    )
    assert R.strip_ids(t) == "Students practice blending daily."
    assert R.strip_ids("Add a quick check at the end (F4, D2).") == "Add a quick check at the end."
    assert R.strip_ids("Keep the routine short.") == "Keep the routine short."


def test_sanitize_summary_then_validates():
    out = {
        "takeaways": [
            {"text": f"Point {i}. As seen in F1 and S1.", "refs": ["F1"], "cites": ["S1"]}
            for i in range(3)
        ]
    }
    assert R.validate_summary(R.sanitize_summary(out, RESEARCH), {"F1"}, RESEARCH) == []


def test_sanitize_combine_fixes_a_misspelled_single_id():
    out = {
        "verdicts": [
            {
                "question_id": "doc-ms standards",
                "verdict": "met",
                "observation": [],
                "suggestions": [],
            }
        ]
    }
    assert (
        R.sanitize_combine(out, {}, {}, "doc-ms-standards")["verdicts"][0]["question_id"]
        == "doc-ms-standards"
    )
