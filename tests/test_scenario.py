"""Practice scenarios, offline: the SME files, the prompts' guard rails, and a whole run
against a fake Nova client (D104-D107)."""

import os

import pytest

from read import scenario as S
from read import scenario_run

os.environ.setdefault("ANSWER_MODEL_ID", "amazon.nova-pro-v1:0")

RESEARCH_TEXT = (
    "Teach blending by having students say each sound and hold the first sound into the next, "
    "a technique known as continuous blending. Model the word yourself first, then have the "
    "group read it with you, then alone. Give feedback on the spot, before the error is "
    "practised."
)
SECOND_TEXT = (
    "When a child reads the sounds correctly but says a different word, the breakdown is in "
    "holding the sounds in memory, not in letter-sound knowledge. Shorten the word, build it "
    "back up one sound at a time, and keep the practice brief and daily."
)

OBJECTIVE = {
    "objective_id": "blend-cvc-k1",
    "component": "Phonics and decoding",
    "grade_band": "K-1",
    "objective": "Students blend three sounds to read CVC words",
    "search_query": "explicit blending instruction CVC words",
}
EXAMPLES = [
    {
        "example_id": "ex-blending-k1",
        "component": "Phonics and decoding",
        "grade_band": "K-1",
        "title": "The stretch that never closes",
        "situation": "A first grader says each sound correctly and then names a different word. "
        "The group is waiting and the rotation is nearly over. " + "Filler sentence here. " * 10,
        "decision_point": "What would you do next?",
        "why_this_is_a_good_example": "Observable behaviour, several defensible answers.",
    },
    {
        "example_id": "ex-retell-23",
        "component": "Comprehension",
        "grade_band": "2-3",
        "situation": "Second graders retell every detail and never reach the problem. "
        + "Filler sentence here. " * 10,
        "decision_point": "What do you do now?",
    },
]

SITUATION = (
    "It is Tuesday in your first grade classroom and five children are at the small table with "
    "you. You point at a short word on a card and ask the group to read it together. One child "
    "says each sound on its own, clearly and in order, pauses, and then names a word that "
    "starts the same way but means something else. She does it twice more on the next two "
    "cards. The other four children are waiting for their turn and the rotation ends in six "
    "minutes."
)


def excerpt(section_id: str, text: str, label: str) -> dict:
    return {
        "cite_id": "",
        "text": text,
        "label": label,
        "ref": {"work_id": "w1", "version_id": "v1", "section_id": section_id},
    }


def fake_search(query, band):
    return [
        excerpt("w1:v1:s0001", RESEARCH_TEXT, "IES Practice Guide, 2016, p. 14"),
        excerpt("w1:v1:s0002", SECOND_TEXT, "National Reading Panel, 2000, p. 2-99"),
    ]


ASKED = [{"point_id": "P1", "point": "Model the word, then read it with the group."}]


def sent(text, response=None, cites=None):
    return {"text": text, "response": response or [], "cites": cites or []}


def rsent(text, points, response=None, cites=None):
    return {"text": text, "points": points, "response": response or [], "cites": cites or []}


SUMMARY_OK = {
    "paragraphs": [
        [
            rsent(
                "Modelling the word yourself before she tries again is worth keeping.",
                ["P1"],
                ["R1"],
            ),
            rsent(
                "Moving the group on leaves her without another try at the word.", ["P2"], ["R2"]
            ),
            rsent(
                "Cover the last letter, have her blend the first two sounds, then uncover it; "
                "the research places this breakdown in holding sounds in memory.",
                ["P2"],
                cites=["S2"],
            ),
        ]
    ],
    "follow_ups": [
        {
            "point_id": "P2",
            "question": "If she says the wrong word again, what would you say first?",
        },
        {"point_id": "P1", "question": "This one is about a point she already did, so drop it?"},
    ],
}
FOLLOW_OK = {
    "did": "partly",
    "reply": [
        sent("You cover the last letter so she hears the first two sounds.", ["R1"]),
        sent("Then uncover it and have her add the last sound back herself.", cites=["S2"]),
    ],
}


class FakeClient:
    """Answers each forced tool call with something that should pass validation."""

    def __init__(self, bad_scenario: bool = False):
        self.calls = []
        self.prompts = []
        self.bad_scenario = bad_scenario

    def converse(self, **kw):
        tool = kw["toolConfig"]["toolChoice"]["tool"]["name"]
        self.calls.append(tool)
        self.prompts.append(kw["messages"][0]["content"][0]["text"])
        if tool == "record_scenario":
            situation = RESEARCH_TEXT if self.bad_scenario and len(self.calls) == 1 else SITUATION
            out = {
                "title": "The stretch that stops short",
                "situation": situation,
                "decision_point": "What would you do with her, and what about the other four?",
                "reflection_prompts": ["What broke down?", "How would you know it worked?"],
                "grounded_in": ["S1", "S2"],
            }
        elif tool == "record_points":
            out = {
                "points": [
                    {
                        "point_id": "P1",
                        "point": "Model the word, then read it with the group.",
                        "cite": "S1",
                    },
                    {
                        "point_id": "P2",
                        "point": "Shorten the word and build it back up.",
                        "cite": "S2",
                    },
                ]
            }
        elif tool == "record_feedback":
            out = {
                "points": [
                    {
                        "point_id": "P1",
                        "did": "did",
                        "observation": [sent("You model the word first.", ["R1"])],
                        "suggestion": [
                            sent(
                                "Next time, after you model it, have the group say it with you "
                                "before she reads it on her own.",
                                cites=["S1"],
                            )
                        ],
                    },
                    {
                        "point_id": "P2",
                        "did": "not mentioned",
                        "observation": [sent("The response does not shorten the word.")],
                        "suggestion": [
                            sent(
                                "Try shortening the word for her and adding the last sound back "
                                "once she has the first two.",
                                cites=["S2"],
                            )
                        ],
                    },
                ],
                "reflection_prompts": [
                    "What would you drop to make time?",
                    "What would you watch for?",
                ],
            }
        elif tool == "record_summary":
            out = SUMMARY_OK
        elif tool == "record_follow_up":
            out = FOLLOW_OK
        elif tool == "record_follow_ups":
            out = {
                "follow_ups": [{"point_id": "P2", "question": "What would you say to her next?"}]
            }
        else:
            raise AssertionError(f"unexpected tool {tool}")
        return {"output": {"message": {"content": [{"toolUse": {"name": tool, "input": out}}]}}}


# ---------------- the SME files ----------------


def test_shipped_objective_and_example_banks_are_valid():
    from read.jobs import DEFAULT_EXAMPLES, DEFAULT_OBJECTIVES

    assert S.check_objectives(S.load_json(DEFAULT_OBJECTIVES)) == []
    assert S.check_examples(S.load_json(DEFAULT_EXAMPLES)) == []


def test_check_objectives_catches_bad_entries():
    bad = {
        "objectives": [
            {"objective_id": "a", "component": "c", "grade_band": "Q9", "objective": "o"}
        ]
    }
    probs = S.check_objectives(bad)
    assert any("search_query" in p for p in probs)
    assert any("grade band" in p for p in probs)
    assert S.check_objectives({"objectives": []}) == ["at least one objective is needed"]


def test_check_examples_wants_at_least_one():
    assert S.check_examples({"examples": []})[0].startswith("at least one worked example")


def test_clean_objective_caps_and_defaults():
    o = S.clean_objective({"objective": "x " * 400, "grade_band": "nonsense"})
    assert len(o["objective"]) <= S.MAX_OBJECTIVE_CHARS
    assert o["grade_band"] == "K-5" and o["custom"] is True
    assert o["search_query"] == o["objective"]


def test_pick_examples_prefers_component_then_band():
    picked = S.pick_examples(EXAMPLES, OBJECTIVE, n=1)
    assert [e["example_id"] for e in picked] == ["ex-blending-k1"]
    other = S.pick_examples(EXAMPLES, {"component": "Comprehension", "grade_band": "2-3"}, n=1)
    assert [e["example_id"] for e in other] == ["ex-retell-23"]


def test_pick_examples_still_returns_something_off_band():
    picked = S.pick_examples(EXAMPLES, {"component": "Fluency", "grade_band": "4-5"}, n=2)
    assert len(picked) == 2  # shape still teaches, and the prompt says the band differs


# ---------------- the response ----------------


def test_response_parts_keep_exact_offsets():
    text = "I would model the word.\n\nThen I would have her read it with me."
    parts = S.response_parts(text)
    assert [p["id"] for p in parts] == ["R1", "R2"]
    for p in parts:
        assert text[p["start"] : p["end"]] == p["text"]


def test_check_response_rejects_too_short_and_too_long():
    assert S.check_response("too short")
    assert S.check_response(" ".join(["word"] * (S.MAX_RESPONSE_WORDS + 1)))
    assert S.check_response(" ".join(["word"] * 50)) == []


# ---------------- generation guard rails ----------------


def test_scenario_must_be_written_in_its_own_words():
    research = {"S1": RESEARCH_TEXT}
    out = {
        "title": "Copied",
        "situation": RESEARCH_TEXT + " " + SITUATION,
        "decision_point": "What next?",
        "reflection_prompts": ["a", "b"],
        "grounded_in": ["S1"],
    }
    probs = S.validate_scenario(out, research, [])
    assert any("own words" in p for p in probs)


def test_scenario_must_ground_in_a_real_section():
    research = {"S1": RESEARCH_TEXT}
    out = {
        "title": "Fine",
        "situation": SITUATION,
        "decision_point": "What next?",
        "reflection_prompts": ["a", "b"],
        "grounded_in": [],
    }
    assert any("grounded_in" in p for p in S.validate_scenario(out, research, []))


def test_sanitize_scenario_drops_unknown_ids_and_id_talk():
    out = S.sanitize_scenario(
        {
            "title": "T",
            "situation": "As S1 says, the child blends.",
            "decision_point": "What next?",
            "reflection_prompts": ["a", "b", "c"],
            "grounded_in": ["S1", "S9"],
        },
        {"S1": RESEARCH_TEXT},
    )
    assert out["grounded_in"] == ["S1"]
    assert len(out["reflection_prompts"]) == S.REFLECTION_PROMPTS


def test_scenario_may_not_mention_a_section_id():
    out = {
        "title": "Fine",
        "situation": "As S1 says, the child blends. " + SITUATION,
        "decision_point": "What next?",
        "reflection_prompts": ["a", "b"],
        "grounded_in": ["S1"],
    }
    probs = S.validate_scenario(out, {"S1": RESEARCH_TEXT}, [])
    assert any("never mentions research or section ids" in p for p in probs)


# ---------------- feedback guard rails ----------------


def test_feedback_observation_may_not_cite_research():
    research, resp = {"S1": RESEARCH_TEXT}, {"R1": "I would model the word."}
    out = {
        "points": [
            {
                "point_id": "P1",
                "did": "did",
                "observation": [sent("You model it.", ["R1"], ["S1"])],
                "suggestion": [sent("Read it together next.", cites=["S1"])],
            }
        ],
        "reflection_prompts": ["a", "b"],
    }
    probs = S.validate_feedback(out, ASKED, {"R1"}, research, resp)
    assert any("observations describe the response only" in p for p in probs)


def test_observation_may_not_be_written_in_the_research_s_words():
    """Live bug: an observation that echoed the research was rejected with 'cite it', which an
    observation is not allowed to do. It must be told to reword instead, and salvaged."""
    research = {"S1": RESEARCH_TEXT}
    resp = {"R1": "I would model the word."}
    echoed = "I would hold the first sound into the next, a technique known as continuous blending."
    out = {
        "points": [
            {
                "point_id": "P1",
                "did": "did",
                "observation": [sent(echoed, ["R1"]), sent("You model the word.", ["R1"])],
                "suggestion": [sent("Then read it with the group.", cites=["S1"])],
            }
        ],
        "reflection_prompts": ["a", "b"],
    }
    probs = S.validate_feedback(out, ASKED, {"R1"}, research, resp)
    assert any("in your own words" in p for p in probs)
    assert not any("without citing it" in p for p in probs)
    kept, notes = S.salvage_feedback(out, ASKED, research)
    assert [x["text"] for x in kept["points"][0]["observation"]] == ["You model the word."]
    assert notes and S.validate_feedback(kept, ASKED, {"R1"}, research, resp) == []


def test_feedback_suggestion_must_cite_research():
    research, resp = {"S1": RESEARCH_TEXT}, {"R1": "I would model the word."}
    out = {
        "points": [
            {
                "point_id": "P1",
                "did": "partly",
                "observation": [sent("You model it.", ["R1"])],
                "suggestion": [sent("Just keep going.")],
            }
        ],
        "reflection_prompts": ["a", "b"],
    }
    probs = S.validate_feedback(out, ASKED, {"R1"}, research, resp)
    assert any("must cite a research section" in p for p in probs)


def test_a_suggestion_that_is_only_a_quote_is_rejected():
    """Live run: a suggestion came back as the research sentence and nothing else, which tells
    the teacher nothing about what to change."""
    research, resp = {"S1": RESEARCH_TEXT}, {"R1": "I would model the word."}
    out = {
        "points": [
            {
                "point_id": "P1",
                "did": "partly",
                "observation": [sent("You model the word.", ["R1"])],
                "suggestion": [
                    sent(
                        "Model the word yourself first, then have the group read it with you.",
                        cites=["S1"],
                    )
                ],
            }
        ],
        "reflection_prompts": ["a", "b"],
    }
    probs = S.validate_feedback(out, ASKED, {"R1"}, research, resp)
    assert any("only a quotation" in p for p in probs)


def test_salvage_drops_a_quote_only_suggestion_instead_of_failing_the_run():
    research, resp = {"S1": RESEARCH_TEXT}, {"R1": "I would model the word."}
    out = {
        "points": [
            {
                "point_id": "P1",
                "did": "partly",
                "observation": [sent("You model the word.", ["R1"])],
                "suggestion": [
                    sent(
                        "Model the word yourself first, then have the group read it with you.",
                        cites=["S1"],
                    ),
                    sent("Keep the practice short and daily after that.", cites=["S1"]),
                ],
            }
        ],
        "reflection_prompts": ["a", "b"],
    }
    kept, notes = S.salvage_feedback(out, ASKED, research)
    assert [x["text"] for x in kept["points"][0]["suggestion"]] == [
        "Keep the practice short and daily after that."
    ]
    assert any("only quoted the research" in n for n in notes)
    assert S.validate_feedback(kept, ASKED, {"R1"}, research, resp) == []


def test_an_observation_may_echo_the_point_it_is_judging():
    """The point is shown above the observation with its own citation, so sharing its wording is
    not the same as passing research off as a description of the response."""
    research, resp = {"S1": RESEARCH_TEXT}, {"R1": "I would model the word."}
    asked = [{"point_id": "P1", "point": "Model the word, then have the group read it with you."}]
    out = {
        "points": [
            {
                "point_id": "P1",
                "did": "did",
                "observation": [
                    sent("You model the word, then have the group read it with you.", ["R1"])
                ],
                "suggestion": [sent("Keep the practice short and daily after that.", cites=["S1"])],
            }
        ],
        "reflection_prompts": ["a", "b"],
    }
    assert S.validate_feedback(out, asked, {"R1"}, research, resp) == []


def test_a_point_the_response_did_must_name_the_part_that_does_it():
    research, resp = {"S1": RESEARCH_TEXT}, {"R1": "I would model the word."}
    out = {
        "points": [
            {
                "point_id": "P1",
                "did": "did",
                "observation": [sent("The teacher models it.")],
                "suggestion": [sent("Keep the practice short and daily.", cites=["S1"])],
            }
        ],
        "reflection_prompts": ["a", "b"],
    }
    probs = S.validate_feedback(out, ASKED, {"R1"}, research, resp)
    assert any("must name the part of the response" in p for p in probs)


def test_feedback_may_not_invent_a_response_part():
    research, resp = {"S1": RESEARCH_TEXT}, {"R1": "I would model the word."}
    out = {
        "points": [
            {
                "point_id": "P1",
                "did": "did",
                "observation": [sent("You say that.", ["R7"])],
                "suggestion": [sent("Read it together.", cites=["S1"])],
            }
        ],
        "reflection_prompts": ["a", "b"],
    }
    probs = S.validate_feedback(out, ASKED, {"R1"}, research, resp)
    assert any("not a part of the response" in p for p in probs)


def test_points_must_cite_a_known_section():
    probs = S.validate_points(
        {"points": [{"point_id": "P1", "point": "Do it.", "cite": "S9"}]}, {"S1": RESEARCH_TEXT}
    )
    assert any("unknown" in p for p in probs)


def test_salvage_points_keeps_the_ones_that_stand_up():
    """Live bug: one point echoed a section it did not cite and the whole run failed, so the
    teacher got nothing. The good points survive now."""
    research = {"S1": RESEARCH_TEXT, "S2": SECOND_TEXT}
    echo = " ".join(SECOND_TEXT.split()[:12])
    out = {
        "points": [
            {"point_id": "P1", "point": echo, "cite": "S1"},
            {"point_id": "P2", "point": "Keep the practice brief.", "cite": "S2"},
        ]
    }
    kept, notes = S.salvage_points(out, research)
    assert [p["point"] for p in kept["points"]] == ["Keep the practice brief."]
    assert kept["points"][0]["point_id"] == "P1"  # renumbered, so feedback can refer to it
    assert notes and S.validate_points(kept, research) == []


def test_show_slices_quotes_from_the_response():
    texts = {"R1": "I would model the word myself before the group reads it with me."}
    shown = S.show(
        [sent("You would model the word myself before the group reads it with me.", ["R1"])], texts
    )
    quotes = [x["quote"] for x in shown[0]["parts"] if "quote" in x]
    assert quotes and quotes[0] in texts["R1"]


# ---------------- whole runs ----------------


def test_generate_builds_a_grounded_scenario():
    client = FakeClient()
    out = scenario_run.generate(OBJECTIVE, EXAMPLES, {"search": fake_search, "client": client})
    assert out["ok"], out.get("problems")
    assert out["scenario"]["grounded_in"] == ["S1", "S2"]
    assert out["examples_used"] == ["ex-blending-k1", "ex-retell-23"]
    assert client.calls == ["record_scenario"]
    assert set(out["research"]) == {"S1", "S2"}


def test_generate_retries_a_copied_scenario_without_the_examples():
    """Live bug: with a worked example in the same component and band, the model rewrote the
    example and both attempts were rejected for copying. The retry drops the examples and
    states the shape in words instead."""
    client = FakeClient(bad_scenario=True)
    out = scenario_run.generate(OBJECTIVE, EXAMPLES, {"search": fake_search, "client": client})
    assert out["ok"]
    assert client.calls == ["record_scenario", "record_scenario"]  # copied wording rejected once
    assert out["scenario"]["situation"] == SITUATION
    assert "<example" in client.prompts[0]
    assert "<example" not in client.prompts[1] and "Shape of a scenario" in client.prompts[1]


def test_generate_without_research_says_so():
    out = scenario_run.generate(
        OBJECTIVE, EXAMPLES, {"search": lambda q, b: [], "client": FakeClient()}
    )
    assert not out["ok"] and "No research" in out["problems"][0]


def test_evaluate_gives_feedback_per_point_with_verified_quotes():
    client = FakeClient()
    deps = {"search": fake_search, "client": client}
    record = scenario_run.generate(OBJECTIVE, EXAMPLES, deps)
    response = (
        "I would model the word myself first and say each sound slowly.\n\n"
        "Then I would ask the rest of the group to read the next card while I work with her."
    )
    out = scenario_run.evaluate(record, response, deps)
    assert out["ok"], out.get("problems")
    assert [p["point_id"] for p in out["points"]] == ["P1", "P2"]
    assert [p["did"] for p in out["points"]] == ["did", "not mentioned"]
    assert out["reflection_prompts"] == [
        "What would you drop to make time?",
        "What would you watch for?",
    ]
    quoted = [
        x["quote"]
        for p in out["points"]
        for s in p["observation"]
        for x in s["parts"]
        if "quote" in x
    ]
    assert all(q in response for q in quoted)  # rule 1: quotes are sliced from the teacher's text
    assert client.calls == [
        "record_scenario",
        "record_points",
        "record_feedback",
        "record_summary",
    ]
    assert out["stats"]["model_calls"] == 3
    para = out["summary"]["paragraphs"][0]
    assert [x["points"] for x in para] == [["P1"], ["P2"], ["P2"]]
    assert para[2]["cites"] == ["S2"] and "parts" in para[2]


def test_evaluate_survives_a_search_failure():
    def boom(query, band):
        raise RuntimeError("search is down")

    client = FakeClient()
    record = scenario_run.generate(OBJECTIVE, EXAMPLES, {"search": fake_search, "client": client})
    out = scenario_run.evaluate(
        record,
        "I would model the word and then read it with her.",
        {"search": boom, "client": client},
    )
    assert out["ok"]  # the scenario's own sections are enough


@pytest.mark.parametrize("field", ["objective", "scenario", "research"])
def test_evaluate_returns_the_context_even_when_it_fails(field):
    class NoPoints(FakeClient):
        def converse(self, **kw):
            tool = kw["toolConfig"]["toolChoice"]["tool"]["name"]
            if tool == "record_points":
                self.calls.append(tool)
                return {
                    "output": {
                        "message": {
                            "content": [{"toolUse": {"name": tool, "input": {"points": []}}}]
                        }
                    }
                }
            return super().converse(**kw)

    client = NoPoints()
    deps = {"search": fake_search, "client": client}
    record = scenario_run.generate(OBJECTIVE, EXAMPLES, deps)
    out = scenario_run.evaluate(record, "I would model the word and then read it with her.", deps)
    assert not out["ok"] and field in out


# ---------------- the coaching summary (D109) ----------------

PTS = [
    {
        "point_id": "P1",
        "point": "Model the word, then read it with the group.",
        "cite": "S1",
        "did": "did",
        "observation": [sent("You model the word first.", ["R1"])],
        "suggestion": [sent("Have the group say it with you next.", cites=["S1"])],
    },
    {
        "point_id": "P2",
        "point": "Shorten the word and build it back up.",
        "cite": "S2",
        "did": "not mentioned",
        "observation": [sent("The response does not shorten the word.")],
        "suggestion": [sent("Shorten it and add the last sound back.", cites=["S2"])],
    },
]
RESP_IDS = {"R1", "R2"}
RESP_TEXTS = {"R1": "I would model the word myself first.", "R2": "Then I would move on."}
TEXTS = {"S1": RESEARCH_TEXT, "S2": SECOND_TEXT}


def summary():
    import copy

    return copy.deepcopy(SUMMARY_OK)


def check_summary(out, points=PTS):
    return S.validate_summary(out, points, RESP_IDS, TEXTS, RESP_TEXTS)


def test_a_good_review_passes():
    assert check_summary(summary()) == []


def test_every_review_sentence_must_list_its_points():
    out = summary()
    out["paragraphs"][0][1]["points"] = []
    assert any("feedback points it draws on" in p for p in check_summary(out))


def test_review_may_only_cite_research_behind_its_points():
    out = summary()
    out["paragraphs"][0][2]["cites"] = ["S1"]  # S1 backs P1, not P2
    assert any("not research behind its points" in p for p in check_summary(out))


def test_review_rejects_praise_but_not_inside_an_example_line():
    out = summary()
    out["paragraphs"][0][0]["text"] = "Great job modelling the word first."
    assert any("praise" in p for p in check_summary(out))
    out = summary()
    out["paragraphs"][0][2]["text"] = "Say 'Great, now put them together' after each try."
    assert not any("praise" in p for p in check_summary(out))


def test_review_must_give_a_research_backed_change():
    out = summary()
    out["paragraphs"][0][2]["cites"] = []
    assert any("at least one change" in p for p in check_summary(out))


def test_review_must_talk_about_the_response():
    out = summary()
    for x in out["paragraphs"][0]:
        x["response"] = []
    assert any("R ids" in p for p in check_summary(out))


def test_review_length_limits():
    out = summary()
    out["paragraphs"] = out["paragraphs"] * 3
    assert any("at most 2 paragraphs" in p for p in check_summary(out))
    out = summary()
    out["paragraphs"][0] = out["paragraphs"][0][:1]
    assert any("3 to 12 sentences" in p for p in check_summary(out))


def test_sanitize_review_drops_invented_ids_and_accepts_a_flat_paragraph():
    raw = {"paragraphs": summary()["paragraphs"][0]}  # one paragraph sent flat
    raw["paragraphs"][1]["points"] = ["P2", "P9"]
    raw["paragraphs"][1]["response"] = ["R2", "R7"]
    out = S.sanitize_summary(raw, ["P1", "P2"], RESP_IDS, TEXTS)
    assert len(out["paragraphs"]) == 1
    assert out["paragraphs"][0][1]["points"] == ["P2"]
    assert out["paragraphs"][0][1]["response"] == ["R2"]


def test_salvage_review_keeps_the_sentences_that_stand_up():
    raw = summary()
    raw["paragraphs"][0][0]["points"] = []
    kept, notes = S.salvage_summary(raw, PTS, RESP_IDS, TEXTS, RESP_TEXTS)
    assert len(kept["paragraphs"][0]) == 2 and notes
    assert check_summary(kept) == []


def test_a_failed_summary_keeps_the_point_by_point_feedback():
    class NoSummary(FakeClient):
        def converse(self, **kw):
            if kw["toolConfig"]["toolChoice"]["tool"]["name"] == "record_summary":
                self.calls.append("record_summary")
                raise RuntimeError("model unavailable")
            return super().converse(**kw)

    client = NoSummary()
    deps = {"search": fake_search, "client": client}
    record = scenario_run.generate(OBJECTIVE, EXAMPLES, deps)
    out = scenario_run.evaluate(record, "I would model the word and then read it with her.", deps)
    assert out["ok"] and out["points"]
    assert out["summary"] is None and out["summary_problems"]


def test_review_prose_loses_its_ids():
    assert S.strip_refs("Telling him the word (R2) skips practice (P2, S11).") == (
        "Telling him the word skips practice."
    )


def test_review_length_cap():
    out = summary()
    out["paragraphs"][0][2]["text"] = "Cover the last letter and try again. " * 14
    probs = check_summary(out)
    assert any("too long" in p for p in probs)
    out = summary()
    out["paragraphs"] = [out["paragraphs"][0] * 3]
    for x in out["paragraphs"][0]:
        x["text"] = " ".join(["word"] * 20) + "."
    assert any("keep it under" in p for p in check_summary(out))


def test_review_rejects_a_bare_id_in_the_prose():
    out = summary()
    out["paragraphs"][0][1]["text"] = "As P2 says, moving on leaves her without another try."
    assert any("id in the prose" in p for p in check_summary(out))


def test_salvage_keeps_a_long_sentence_rather_than_no_review():
    raw = summary()
    raw["paragraphs"][0][2]["text"] = "Cover the last letter and try again with her. " * 11
    assert any("too long" in p for p in check_summary(raw))
    kept, notes = S.salvage_summary(raw, PTS, RESP_IDS, TEXTS, RESP_TEXTS)
    assert len(kept["paragraphs"][0]) == 3 and check_summary(kept) == []


def test_sanitize_review_drops_a_repeated_reply():
    raw = summary()
    raw["paragraphs"] = raw["paragraphs"] * 2  # the whole reply twice
    out = S.sanitize_summary(raw, ["P1", "P2"], RESP_IDS, TEXTS)
    assert len(out["paragraphs"]) == 1 and len(out["paragraphs"][0]) == 3


def test_salvage_cites_a_quoted_section_behind_the_sentence_s_points_only():
    raw = summary()
    quote = "Shorten the word, build it back up one sound at a time, and keep the practice brief"
    raw["paragraphs"][0][2] = rsent(f"{quote}.", ["P2"])  # quotes S2, forgot to cite it
    assert check_summary(raw)
    kept, _ = S.salvage_summary(raw, PTS, RESP_IDS, TEXTS, RESP_TEXTS)
    assert kept["paragraphs"][0][2]["cites"] == ["S2"]
    raw = summary()
    raw["paragraphs"][0][2] = rsent(f"{quote}.", ["P1"])  # S2 is not behind P1
    kept, notes = S.salvage_summary(raw, PTS, RESP_IDS, TEXTS, RESP_TEXTS)
    assert raw["paragraphs"][0][2]["cites"] == []  # never given a cite its points don't back
    assert not notes  # and with no research-backed sentence left, salvage gives up


def test_sanitize_review_turns_ids_in_a_sentence_into_words():
    raw = summary()
    raw["paragraphs"][0][1]["text"] = "As P2 says, R2 moves on before she tries again (S2)."
    out = S.sanitize_summary(raw, ["P1", "P2"], RESP_IDS, TEXTS)
    assert out["paragraphs"][0][1]["text"] == (
        "As point 2 says, your response moves on before she tries again."
    )


def test_salvage_trims_a_long_reply_from_the_end():
    raw = summary()
    filler = " ".join(["more"] * 80) + "."
    raw["paragraphs"].append([rsent(filler, ["P1"], ["R1"]) for _ in range(3)])
    assert any("keep it under" in p for p in check_summary(raw))
    kept, notes = S.salvage_summary(raw, PTS, RESP_IDS, TEXTS, RESP_TEXTS)
    assert any("trimmed" in n for n in notes) and check_summary(kept) == []
    assert kept["paragraphs"][0] == summary()["paragraphs"][0]  # the important part survives


def test_salvage_drops_a_lead_in_left_dangling_at_the_start():
    raw = summary()
    raw["paragraphs"][0][0]["points"] = []  # the opening sentence fails and is dropped
    raw["paragraphs"][0][1]["text"] = "Additionally, moving on leaves her without another try."
    kept, _ = S.salvage_summary(raw, PTS, RESP_IDS, TEXTS, RESP_TEXTS)
    assert kept["paragraphs"][0][0]["text"] == "Moving on leaves her without another try."


# ---------------- optional follow-up questions (D110) ----------------


def test_follow_ups_keep_only_open_points_in_own_words():
    pts = [dict(PTS[0]), dict(PTS[1]), dict(PTS[1], point_id="P3")]
    ups = [
        {"point_id": "P1", "question": "What would you do next?"},  # P1 was done
        {"point_id": "P2", "question": "What would you say to her first?"},
        {"point_id": "P2", "question": "And then?"},  # one question per point
        {"point_id": "P3", "question": "What does P3 look like here?"},  # an id
        {"point_id": "P3", "question": "No question mark."},
        {"point_id": "P9", "question": "Unknown point?"},
    ]
    assert S.clean_follow_ups(ups, pts, TEXTS) == [
        {"point_id": "P2", "question": "What would you say to her first?"}
    ]
    copied = [
        {
            "point_id": "P2",
            "question": "Would you hold the first sound into the next, a technique known as "
            "continuous blending?",
        }
    ]
    assert S.clean_follow_ups(copied, pts, TEXTS) == []  # research wording gives the answer away


def test_check_answer_limits():
    assert S.check_answer("too short")
    assert S.check_answer("word " * 400)
    assert S.check_answer("I would cover the last letter first.") == []


def test_follow_up_reply_needs_research_and_names_the_answer():
    resp = {"R1"}
    rt = {"R1": "I would cover the last letter."}
    assert S.validate_follow(FOLLOW_OK, resp, TEXTS, rt) == []
    no_cite = {"did": "partly", "reply": [sent("You cover it.", ["R1"]), sent("Then go on.")]}
    assert any("citing it" in p for p in S.validate_follow(no_cite, resp, TEXTS, rt))
    unnamed = {"did": "did", "reply": [sent("You cover it."), sent("Add it back.", cites=["S2"])]}
    assert any("R id" in p for p in S.validate_follow(unnamed, resp, TEXTS, rt))


def test_evaluate_offers_follow_ups_and_answers_one():
    client = FakeClient()
    deps = {"search": fake_search, "client": client}
    record = scenario_run.generate(OBJECTIVE, EXAMPLES, deps)
    fb = scenario_run.evaluate(record, "I would model the word and then read it with her.", deps)
    assert fb["follow_ups"] == [SUMMARY_OK["follow_ups"][0]]  # the P1 question was dropped
    out = scenario_run.follow_up(record, fb, "P2", "I would cover the last letter first.", deps)
    assert out["ok"], out.get("problems")
    assert out["question"] == SUMMARY_OK["follow_ups"][0]["question"]  # from the stored feedback
    assert out["did"] == "partly" and out["reply"][1]["cites"] == ["S2"]
    assert client.calls[-1] == "record_follow_up"
    gone = scenario_run.follow_up(record, fb, "P1", "I would cover the last letter first.", deps)
    assert not gone["ok"]  # no question was asked about P1


def test_sentences_split_prose_but_not_a_quoted_example_line():
    assert S.sentences("You model it. Then she tries. Good.") == [
        "You model it.",
        "Then she tries.",
        "Good.",
    ]
    assert S.sentences("Say 'Now you try. Blend it smoothly.' Then wait.") == [
        "Say 'Now you try. Blend it smoothly.'",
        "Then wait.",
    ]
    assert S.sentences("Don't move on. Ask again.") == ["Don't move on.", "Ask again."]
    assert S.sentences('Say "Try it. Go on." Then listen.') == [
        'Say "Try it. Go on."',
        "Then listen.",
    ]


def test_sanitize_splits_a_packed_item_and_keeps_its_ids():
    raw = {
        "paragraphs": [
            [rsent("You model the word. She never tries it herself. Let her try.", ["P2"], ["R1"])]
        ]
    }
    out = S.sanitize_summary(raw, ["P1", "P2"], RESP_IDS, TEXTS)
    para = out["paragraphs"][0]
    assert [x["text"] for x in para] == [
        "You model the word.",
        "She never tries it herself.",
        "Let her try.",
    ]
    assert all(x["points"] == ["P2"] and x["response"] == ["R1"] for x in para)


def test_sentences_keep_an_apostrophe_inside_an_example_line():
    assert S.sentences("Say 'Now you try. Let's sound it out.' Then wait.") == [
        "Say 'Now you try. Let's sound it out.'",
        "Then wait.",
    ]


def test_salvage_review_prunes_a_cite_its_points_do_not_back():
    raw = summary()
    raw["paragraphs"][0][2]["cites"] = ["S2", "S1"]  # S1 is behind P1, not P2
    kept, _ = S.salvage_summary(raw, PTS, RESP_IDS, TEXTS, RESP_TEXTS)
    assert kept["paragraphs"][0][2]["cites"] == ["S2"] and check_summary(kept) == []


def test_salvage_follow_up_drops_a_praise_sentence():
    rt = {"R1": "I would cover the last letter."}
    raw = {"did": "partly", "reply": [sent("Great idea to cover it.", ["R1"])] + FOLLOW_OK["reply"]}
    assert S.validate_follow(raw, {"R1"}, TEXTS, rt)
    kept, notes = S.salvage_follow(raw, {"R1"}, TEXTS, rt)
    assert len(kept["reply"]) == 2 and notes
    assert S.validate_follow(kept, {"R1"}, TEXTS, rt) == []


def test_questions_written_into_the_review_move_to_the_follow_ups():
    raw = summary()
    raw["paragraphs"].append([rsent("If she guesses again, what would you say first?", ["P2"])])
    raw["paragraphs"][0][2]["text"] = "Ask her 'What sound comes next?' and wait."
    out = S.sanitize_summary(raw, ["P1", "P2"], RESP_IDS, TEXTS)
    assert len(out["paragraphs"]) == 1  # the question-only paragraph is gone
    assert out["paragraphs"][0][2]["text"] == "Ask her 'What sound comes next?' and wait."
    assert out["follow_ups"][-1] == {
        "point_id": "P2",
        "question": "If she guesses again, what would you say first?",
    }


def test_salvage_follow_up_drops_a_dangling_lead_in():
    rt = {"R1": "I would cover the last letter."}
    raw = {
        "did": "partly",
        "reply": [
            sent("Great idea to cover it.", ["R1"]),
            sent("However, add the last sound back once she has two.", cites=["S2"]),
        ],
    }
    kept, _ = S.salvage_follow(raw, {"R1"}, TEXTS, rt)
    assert kept["reply"][0]["text"] == "Add the last sound back once she has two."


def test_follow_ups_are_asked_separately_when_the_review_leaves_them_out():
    class NoQuestions(FakeClient):
        def converse(self, **kw):
            res = super().converse(**kw)
            if kw["toolConfig"]["toolChoice"]["tool"]["name"] == "record_summary":
                inp = res["output"]["message"]["content"][0]["toolUse"]["input"]
                inp = {k: v for k, v in inp.items() if k != "follow_ups"}
                res["output"]["message"]["content"][0]["toolUse"]["input"] = inp
            return res

    client = NoQuestions()
    deps = {"search": fake_search, "client": client}
    record = scenario_run.generate(OBJECTIVE, EXAMPLES, deps)
    out = scenario_run.evaluate(record, "I would model the word and then read it with her.", deps)
    assert client.calls[-1] == "record_follow_ups"
    assert out["follow_ups"] == [{"point_id": "P2", "question": "What would you say to her next?"}]
    assert out["summary"] is not None
