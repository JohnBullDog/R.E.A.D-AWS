"""Run a practice scenario: generate one from a learning objective, then give feedback on the
teacher's response. Dependencies are passed in so tests can fake them.

deps = {
  "search": (query, grade_band) -> verified excerpts from the research library,
  "client": bedrock-runtime client for the Nova calls,
}

Generation is one search and one model call, so it answers inside an API request. Evaluation is
one search and three calls (points, feedback, coaching summary, D109) and runs as a worker job
(D107), because API Gateway waits at most 30 seconds.
"""

import time

from read import review as R
from read import scenario as S

SCENARIO_TOKENS = 1200
POINTS_TOKENS = 900
FEEDBACK_TOKENS = 2000
SUMMARY_TOKENS = 1600
FOLLOW_TOKENS = 700
STEPS = 4


def _number(research: dict, by_section: dict, excerpts: list[dict]) -> list[str]:
    """Add excerpts to the S1, S2, ... numbering, keeping ids already handed out."""
    ids = []
    for x in excerpts:
        sid = x["ref"]["section_id"]
        if sid not in by_section:
            by_section[sid] = f"S{len(by_section) + 1}"
            research[by_section[sid]] = {**x, "cite_id": by_section[sid]}
        ids.append(by_section[sid])
    return list(dict.fromkeys(ids))


def _items(research: dict, ids: list[str]) -> list[dict]:
    return [{"cite_id": i, "text": research[i]["text"]} for i in ids if i in research]


def generate(
    objective: dict, examples: list[dict], deps: dict, avoid: list[str] | None = None
) -> dict:
    """One scenario for this objective, grounded in the research found for it."""
    t0 = time.time()
    research: dict[str, dict] = {}
    by_section: dict[str, str] = {}
    query = f"{objective.get('search_query') or objective['objective']}. {objective['objective']}"
    found = deps["search"](query, objective.get("grade_band", "K-5"))[: S.SCENARIO_RESEARCH]
    ids = _number(research, by_section, found)
    if not ids:
        return {
            "ok": False,
            "objective": objective,
            "research": {},
            "attempts": [],
            "problems": [
                "No research in the library covers this objective, so a scenario would not be "
                "grounded in anything. Try another objective, or add a source."
            ],
        }
    picked = S.pick_examples(examples, objective)
    texts = {k: v["text"] for k, v in research.items()}
    items = _items(research, ids)
    prompt = S.scenario_prompt(objective, items, picked, avoid or [])
    # An example in the same component and grade band is the strongest thing in the prompt, and
    # the model rewrites it; when that trips the copy check, the retry goes without examples.
    bare = S.scenario_prompt(objective, items, [], avoid or [])

    def ask(fb):
        return S.sanitize_scenario(
            R.converse(
                deps["client"],
                S.SCENARIO_SYSTEM,
                (bare if fb else prompt) + (f"\n\n{fb}" if fb else ""),
                "record_scenario",
                S.SCENARIO_SCHEMA,
                SCENARIO_TOKENS,
            ),
            texts,
        )

    res = R.with_retry(ask, lambda o: S.validate_scenario(o, texts, picked))
    out = {
        "ok": res["ok"],
        "attempts": res["attempts"],
        "objective": objective,
        "research": research,
        "examples_used": [e.get("example_id") for e in picked],
        "stats": {"model_calls": len(res["attempts"]), "seconds": round(time.time() - t0, 1)},
    }
    if res["ok"]:
        out["scenario"] = res["result"]
    else:
        out["problems"] = res.get("problems", [])
    return out


def evaluate(
    record: dict,
    response_text: str,
    deps: dict,
    prior: list[dict] | None = None,
    progress=lambda **kw: None,
) -> dict:
    """Feedback on one response: what the research says to do at this decision point, then how
    the response met each of those points. Nothing is scored."""
    t0 = time.time()
    objective, scenario = record["objective"], record["scenario"]
    research = {k: dict(v) for k, v in record["research"].items()}
    by_section = {v["ref"]["section_id"]: k for k, v in research.items()}
    parts = S.response_parts(response_text)
    resp_ids = {p["id"] for p in parts}
    resp_texts = {p["id"]: p["text"] for p in parts}
    calls = 0

    progress(phase="Finding research for your response", done=0, total=STEPS)
    query = f"{objective['objective']}. {' '.join(response_text.split()[:120])}"
    try:
        found = deps["search"](query, objective.get("grade_band", "K-5"))[: S.EVAL_RESEARCH]
    except Exception:  # the scenario's own sections are enough to give feedback
        found = []
    ids = list(
        dict.fromkeys(
            [c for c in scenario.get("grounded_in", []) if c in research]
            + _number(research, by_section, found)
        )
    )
    research_texts = {k: v["text"] for k, v in research.items()}

    progress(phase="Reading what the research says", done=1, total=STEPS)
    pprompt = S.points_prompt(objective, scenario, _items(research, ids))

    def ask_points(fb):
        return S.sanitize_points(
            R.converse(
                deps["client"],
                S.POINTS_SYSTEM,
                pprompt + (f"\n\n{fb}" if fb else ""),
                "record_points",
                S.POINTS_SCHEMA,
                POINTS_TOKENS,
            ),
            research_texts,
        )

    pres = R.with_retry(
        ask_points,
        lambda o: S.validate_points(o, research_texts),
        lambda o: S.salvage_points(o, research_texts),
    )
    calls += len(pres["attempts"])
    base = {
        "objective": objective,
        "scenario": scenario,
        "response": response_text,
        "parts": parts,
        "research": research,
    }
    if not pres["ok"]:
        return {
            **base,
            "ok": False,
            "problems": pres.get("problems", []),
            "attempts": pres["attempts"],
            "stats": {"model_calls": calls, "seconds": round(time.time() - t0, 1)},
        }
    points = pres["result"]["points"]
    cited = sorted({p["cite"] for p in points}, key=lambda c: int(c[1:]))
    used_texts = {c: research_texts[c] for c in cited}
    need = list(points)  # the points still expected; salvage may prune it

    progress(phase="Giving feedback on your response", done=2, total=STEPS)
    fprompt = S.feedback_prompt(
        objective, scenario, points, parts, _items(research, cited), prior or []
    )

    def ask_feedback(fb):
        return S.sanitize_feedback(
            R.converse(
                deps["client"],
                S.FEEDBACK_SYSTEM,
                fprompt + (f"\n\n{fb}" if fb else ""),
                "record_feedback",
                S.FEEDBACK_SCHEMA,
                FEEDBACK_TOKENS,
            ),
            [x["point_id"] for x in need],
            resp_ids,
            used_texts,
        )

    def check(o):
        return S.validate_feedback(o, need, resp_ids, used_texts, resp_texts)

    def salvage(o):
        kept, notes = S.salvage_feedback(o, need, used_texts)
        survived = {p["point_id"] for p in kept.get("points", [])}
        need[:] = [x for x in need if x["point_id"] in survived]
        return kept, notes

    fres = R.with_retry(ask_feedback, check, salvage)
    calls += len(fres["attempts"])
    progress(phase="Writing the coaching review", done=3, total=STEPS)
    out = {
        **base,
        "ok": fres["ok"],
        "attempts": fres["attempts"],
        "fixes": pres.get("salvaged", []) + fres.get("salvaged", []),
        "stats": {"model_calls": calls, "seconds": round(time.time() - t0, 1)},
    }
    if not fres["ok"]:
        out["problems"] = fres.get("problems", [])
        return out
    by_point = {p["point_id"]: p for p in fres["result"].get("points", [])}
    texts_all = resp_texts | research_texts
    out["points"] = [
        {
            "point_id": p["point_id"],
            "point": p["point"],
            "cite": p["cite"],
            "did": by_point[p["point_id"]]["did"],
            "observation": S.show(by_point[p["point_id"]]["observation"], texts_all),
            "suggestion": S.show(by_point[p["point_id"]]["suggestion"], texts_all),
        }
        for p in points
        if p["point_id"] in by_point
    ]
    out["reflection_prompts"] = fres["result"].get("reflection_prompts", [])
    out["dropped_points"] = [p["point"] for p in points if p["point_id"] not in by_point]

    # The coaching review (D109) is written from the checked feedback only. It fails soft: the
    # point-by-point feedback above stands on its own.
    sres = summarize(objective, scenario, parts, out["points"], research, deps)
    calls += len(sres["attempts"])
    if sres["ok"]:
        texts_all = resp_texts | research_texts
        out["summary"] = {
            "paragraphs": [S.show(para, texts_all) for para in sres["result"]["paragraphs"]]
        }
        offered = sres["result"].get("follow_ups") or []
        out["follow_ups"] = S.clean_follow_ups(offered, out["points"], research_texts)
        if len(out["follow_ups"]) < len(offered):
            out["fixes"].append(
                f"follow-ups: kept {len(out['follow_ups'])} of {len(offered)}: "
                + "; ".join(
                    str(u.get("question", ""))[:80] for u in offered if u not in out["follow_ups"]
                )
            )
        out["fixes"] += sres.get("salvaged", [])
    else:
        out["summary"] = None
        out["summary_problems"] = sres.get("problems", [])
    out["attempts"] = out["attempts"] + [{"summary": a} for a in sres["attempts"]]
    if not out.get("follow_ups") and any(p["did"] != "did" for p in out["points"]):
        # Nova often leaves the optional questions out of the review call; ask for them alone
        ups, n = ask_follow_ups(objective, scenario, parts, out["points"], research_texts, deps)
        calls += n
        out["follow_ups"] = ups
        out["fixes"].append(f"follow-ups: asked separately, {len(ups)} kept")
    out["stats"] = {"model_calls": calls, "seconds": round(time.time() - t0, 1)}
    progress(phase="Writing the coaching review", done=STEPS, total=STEPS)
    return out


def summarize(
    objective: dict,
    scenario: dict,
    parts: list[dict],
    points: list[dict],
    research: dict,
    deps: dict,
) -> dict:
    """A one- or two-paragraph reply in a reviewer's voice, from the checked points."""
    if not points:
        return {"ok": False, "problems": ["no checked feedback to summarise"], "attempts": []}
    resp_ids = {p["id"] for p in parts}
    resp_texts = {p["id"]: p["text"] for p in parts}
    cited = sorted(set().union(*S.allowed_cites(points).values()), key=lambda c: int(c[1:]))
    texts = {c: research[c]["text"] for c in cited if c in research}
    prompt = S.summary_prompt(objective, scenario, parts, points, _items(research, cited))
    point_ids = [p["point_id"] for p in points]

    def ask(fb):
        return S.sanitize_summary(
            R.converse(
                deps["client"],
                S.SUMMARY_SYSTEM,
                prompt + (f"\n\n{fb}" if fb else ""),
                "record_summary",
                S.SUMMARY_SCHEMA,
                SUMMARY_TOKENS,
            ),
            point_ids,
            resp_ids,
            texts,
        )

    try:
        res = R.with_retry(
            ask,
            lambda o: S.validate_summary(o, points, resp_ids, texts, resp_texts),
            lambda o: S.salvage_summary(o, points, resp_ids, texts, resp_texts),
        )
    except Exception as exc:  # never lose the point-by-point feedback over the summary
        return {"ok": False, "problems": [f"{type(exc).__name__}: {exc}"[:300]], "attempts": []}
    if res["ok"]:
        res["result"].pop("salvaged_from", None)
    return res


def follow_up(record: dict, feedback: dict, point_id: str, answer: str, deps: dict) -> dict:
    """Feedback on the teacher's answer to one optional follow-up question (D110): one call,
    read against that point and the research behind it only. The question is taken from the
    stored feedback, never from the browser."""
    t0 = time.time()
    point = next((p for p in feedback.get("points", []) if p["point_id"] == point_id), None)
    ask = next((u for u in feedback.get("follow_ups", []) if u.get("point_id") == point_id), None)
    if not point or not ask:
        return {"ok": False, "problems": ["that follow-up question is not part of this feedback"]}
    research = feedback["research"]
    cited = sorted(S.allowed_cites([point])[point_id], key=lambda c: int(c[1:]))
    texts = {c: research[c]["text"] for c in cited if c in research}
    parts = S.response_parts(answer)
    resp_ids = {p["id"] for p in parts}
    resp_texts = {p["id"]: p["text"] for p in parts}
    prompt = S.follow_up_prompt(
        record["objective"],
        record["scenario"],
        point,
        ask["question"],
        parts,
        _items(research, list(texts)),
    )

    def call(fb):
        return S.sanitize_follow(
            R.converse(
                deps["client"],
                S.FOLLOW_SYSTEM,
                prompt + (f"\n\n{fb}" if fb else ""),
                "record_follow_up",
                S.FOLLOW_SCHEMA,
                FOLLOW_TOKENS,
            ),
            resp_ids,
            texts,
        )

    res = R.with_retry(
        call,
        lambda o: S.validate_follow(o, resp_ids, texts, resp_texts),
        lambda o: S.salvage_follow(o, resp_ids, texts, resp_texts),
    )
    out = {
        "ok": res["ok"],
        "point_id": point_id,
        "question": ask["question"],
        "attempts": res["attempts"],
        "stats": {"model_calls": len(res["attempts"]), "seconds": round(time.time() - t0, 1)},
    }
    if res["ok"]:
        out["did"] = res["result"]["did"]
        out["reply"] = S.show(res["result"]["reply"], resp_texts | texts)
    else:
        out["problems"] = res.get("problems", [])
    return out


def ask_follow_ups(
    objective: dict,
    scenario: dict,
    parts: list[dict],
    points: list[dict],
    research_texts: dict,
    deps: dict,
) -> tuple[list[dict], int]:
    """The follow-up questions on their own (D110), when the review call left them out. One
    call, no retry: they are optional, and the questions that don't pass are dropped."""
    try:
        raw = R.converse(
            deps["client"],
            S.FOLLOW_UPS_SYSTEM,
            S.follow_ups_prompt(objective, scenario, parts, points),
            "record_follow_ups",
            S.FOLLOW_UPS_SCHEMA,
            FOLLOW_TOKENS,
        )
    except Exception:  # optional: the page falls back to the reflection prompts
        return [], 1
    ups = raw.get("follow_ups") if isinstance(raw, dict) else None
    clean = [
        {"point_id": str(u.get("point_id", "")), "question": " ".join(str(u["question"]).split())}
        for u in (ups if isinstance(ups, list) else [])
        if isinstance(u, dict) and isinstance(u.get("question"), str)
    ]
    return S.clean_follow_ups(clean, points, research_texts), 1
