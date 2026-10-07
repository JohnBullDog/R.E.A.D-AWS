"""Run a v2 material review end to end. Dependencies are passed in so tests can fake them.

deps = {
  "embed": text -> normalized vector (Titan),
  "search": (query, goal) -> verified excerpts from /search (cite_id, text, label, ref, ...),
  "client": bedrock-runtime client for the Nova calls,
}
"""

import time
from concurrent.futures import ThreadPoolExecutor

from read import review as R

WORKERS = 4
_QUESTION_VECS: dict[str, list[float]] = {}  # question text -> vector, reused across reviews


def run(
    material: dict,
    goal: dict,
    chosen_b: set[str],
    checklist: dict,
    deps: dict,
    progress=lambda **kw: None,
) -> dict:
    t0 = time.time()
    parts, sections = material["parts"], material["sections"]
    parts_by_id = {p["id"]: p for p in parts}
    list_a, list_b = R.split_checklist(checklist["criteria"], goal["grade_band"])
    qb = [
        {
            "id": q["criterion_id"],
            "question": q["question"],
            "rule": q["combine"],
            "core": bool(q.get("core")),
            "component": q["component"],
            "search_query": q["search_query"],
        }
        for q in list_b
        if q.get("core") or q["criterion_id"] in chosen_b
    ]
    skipped_b = [
        q["criterion_id"] for q in list_b if not q.get("core") and q["criterion_id"] not in chosen_b
    ]
    a_by_id = {q["criterion_id"]: q for q in list_a}

    # 3. triage
    progress(phase="Matching questions to sections", done=0, total=len(sections))
    for q in list_a:
        key = q["question"] + " " + q["search_query"]
        if key not in _QUESTION_VECS:
            _QUESTION_VECS[key] = deps["embed"](key)
    qvecs = {
        q["criterion_id"]: _QUESTION_VECS[q["question"] + " " + q["search_query"]] for q in list_a
    }
    with ThreadPoolExecutor(WORKERS) as pool:
        svecs = list(
            pool.map(
                lambda s: deps["embed"](
                    " ".join(parts_by_id[i]["text"] for i in s["parts"])[:6000]
                ),
                sections,
            )
        )
    tri = [R.triage(v, qvecs) if qvecs else {} for v in svecs]
    offered = [[qid for qid, t in m.items() if t["label"] != "out"] for m in tri]

    # 4. research once per chosen question; S ids shared across the review
    wanted = sorted({q for o in offered for q in o}) + [q["id"] for q in qb]
    progress(phase="Finding research", done=0, total=len(wanted))
    research: dict[str, dict] = {}  # S id -> excerpt
    by_section_id: dict[str, str] = {}
    q_research: dict[str, list[str]] = {}

    def fetch(qid):
        q = a_by_id.get(qid) or next(x for x in qb if x["id"] == qid)
        return qid, deps["search"](f"{q['search_query']}. {goal['objective']}", goal)

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(fetch, wanted))
    for qid, excerpts in results:  # number in a stable order
        ids = []
        for x in excerpts:
            sid = x["ref"]["section_id"]
            if sid not in by_section_id:
                by_section_id[sid] = f"S{len(by_section_id) + 1}"
                research[by_section_id[sid]] = {**x, "cite_id": by_section_id[sid]}
            ids.append(by_section_id[sid])
        q_research[qid] = ids
    texts_all = {p["id"]: p["text"] for p in parts} | {k: v["text"] for k, v in research.items()}

    # 5. one call per section
    progress(phase="Reviewing sections", done=0, total=len(sections))
    done = {"n": 0}

    def review_section(k):
        sec = sections[k]
        qa = [a_by_id[q] for q in offered[k]]
        sec_research = sorted(
            {s for q in offered[k] for s in q_research.get(q, [])}, key=lambda s: int(s[1:])
        )
        items = [{"cite_id": s, "text": research[s]["text"]} for s in sec_research]
        labels = {q: tri[k][q]["label"] for q in offered[k]}
        prompt = R.section_prompt(goal, sections, sec, parts_by_id, qa, labels, items, qb)
        res_map = {s: research[s]["text"] for s in sec_research}
        qa_ids, qb_ids = set(offered[k]), {q["id"] for q in qb}
        fixes: list[str] = []

        def ask(fb):
            raw = R.converse(
                deps["client"],
                R.SECTION_SYSTEM,
                prompt + (f"\n\n{fb}" if fb else ""),
                "record_section",
                R.SECTION_SCHEMA,
                2500,
            )
            clean, notes = R.sanitize_section(raw, qa_ids, qb_ids, sec, res_map, texts_all, labels)
            fixes[:] = notes
            return clean

        out = R.with_retry(
            ask, lambda o: R.validate_section(o, sec, qa_ids, qb_ids, texts_all, res_map)
        )
        out["fixes"] = list(fixes)
        done["n"] += 1
        progress(phase="Reviewing sections", done=done["n"], total=len(sections))
        return out

    def safe_section(k):  # one broken section must not stop the review
        try:
            return review_section(k)
        except Exception as exc:
            return {"ok": False, "problems": [f"{type(exc).__name__}: {exc}"[:300]], "attempts": []}

    with ThreadPoolExecutor(WORKERS) as pool:
        sec_results = list(pool.map(safe_section, range(len(sections))))

    findings, sections_out = [], []
    per_question = {q["id"]: {"status": None, "answers": []} for q in qb}
    for k, (sec, res) in enumerate(zip(sections, sec_results, strict=True)):
        entry = {
            "id": sec["id"],
            "title": sec["title"],
            "parts": sec["parts"],
            "ok": res["ok"],
            "triage": tri[k],
            "attempts": res["attempts"],
            "fixes": res.get("fixes", []),
            "findings": [],
        }
        if res["ok"]:
            out = R.dedupe_overlap(res["result"], sec)
            for f in out["findings"]:
                q = a_by_id[f["question_id"]]
                fid = f"F{len(findings) + 1}"
                cites = sorted({c for s in f["observation"] + f["suggestions"] for c in s["cites"]})
                item = {
                    "id": fid,
                    "question_id": q["criterion_id"],
                    "question": q["question"],
                    "component": q["component"],
                    "section_title": sec["title"],
                    "seen": f["seen"],
                    "observation": f["observation"],
                    "suggestions": f["suggestions"],
                    "cites": cites,
                }
                findings.append(item)
                entry["findings"].append(
                    {
                        **item,
                        "observation": R.show(f["observation"], texts_all),
                        "suggestions": R.show(f["suggestions"], texts_all),
                    }
                )
            for d in out["doc"]:
                if d["question_id"] in per_question:
                    per_question[d["question_id"]]["answers"].append(
                        {
                            "section": sec["title"],
                            "answer": d["answer"],
                            "material": d["material"],
                            "note": " ".join(d.get("note", "").split())[:200],
                        }
                    )
        else:
            entry["problems"] = res["problems"]
        sections_out.append(entry)
    unreviewed = [s["title"] for s, r in zip(sections, sec_results, strict=True) if not r["ok"]]

    # 6. combine whole-document answers
    verdicts, document_out = [], []
    combine_res = {"ok": True, "attempts": []}
    if qb:
        progress(phase="Combining whole-document checks", done=0, total=1)
        for q in qb:
            per_question[q["id"]]["status"] = R.combine_status(
                q["rule"], [a["answer"] for a in per_question[q["id"]]["answers"]]
            )
        mats = {p["id"] for p in parts}
        all_parts = {p["id"]: p["text"] for p in parts}

        def combine_one(q):  # one small call per question: Nova won't fill many entries at once
            q_res = sorted(q_research.get(q["id"], []), key=lambda s: int(s[1:]))
            items = [{"cite_id": s, "text": research[s]["text"]} for s in q_res]
            prompt = R.combine_prompt(goal, [q], per_question, items)
            res_map = {s: research[s]["text"] for s in q_res}
            try:
                return R.with_retry(
                    lambda fb: R.sanitize_combine(
                        R.converse(
                            deps["client"],
                            R.COMBINE_SYSTEM,
                            prompt + (f"\n\n{fb}" if fb else ""),
                            "record_verdicts",
                            R.COMBINE_SCHEMA,
                            1200,
                        ),
                        res_map,
                        all_parts,
                        q["id"],
                    ),
                    lambda o: R.validate_combine(
                        o, {q["id"]}, per_question, mats, texts_all, res_map
                    ),
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "problems": [f"{type(exc).__name__}: {exc}"[:300]],
                    "attempts": [],
                }

        with ThreadPoolExecutor(WORKERS) as pool:
            combined = list(pool.map(combine_one, qb))
        combine_res = {
            "ok": all(c["ok"] for c in combined),
            "attempts": [a for c in combined for a in c["attempts"]],
        }
        got = {}
        for c in combined:
            for v in c.get("result", {}).get("verdicts", []) if c["ok"] else []:
                got[v["question_id"]] = v
        for q in qb:
            v = got.get(q["id"])
            row = {
                "question_id": q["id"],
                "question": q["question"],
                "core": q["core"],
                "component": q["component"],
                "answers": per_question[q["id"]]["answers"],
                "status": per_question[q["id"]]["status"],
            }
            if v:
                did = f"D{len(verdicts) + 1}"
                cites = sorted({c for s in v["observation"] + v["suggestions"] for c in s["cites"]})
                verdicts.append(
                    {
                        "id": did,
                        "question": q["question"],
                        "verdict": v["verdict"],
                        "observation": v["observation"],
                        "suggestions": v["suggestions"],
                        "cites": cites,
                    }
                )
                row |= {
                    "id": did,
                    "verdict": v["verdict"],
                    "observation": R.show(v["observation"], texts_all),
                    "suggestions": R.show(v["suggestions"], texts_all),
                }
            document_out.append(row)

    # 7. summary
    progress(phase="Writing the summary", done=0, total=1)
    summary = {"ok": False, "takeaways": [], "attempts": []}
    if findings or verdicts:
        cited = sorted(
            {c for x in findings + verdicts for c in x["cites"]}, key=lambda s: int(s[1:])
        )
        items = [{"cite_id": s, "text": research[s]["text"]} for s in cited]
        prompt = R.summary_prompt(goal, findings, verdicts, items)
        res_map = {s: research[s]["text"] for s in cited}
        ref_ids = {x["id"] for x in findings + verdicts}
        sres = R.with_retry(
            lambda fb: R.sanitize_summary(
                R.converse(
                    deps["client"],
                    R.SUMMARY_SYSTEM,
                    prompt + (f"\n\n{fb}" if fb else ""),
                    "record_summary",
                    R.SUMMARY_SCHEMA,
                    1200,
                ),
                res_map,
            ),
            lambda o: R.validate_summary(o, ref_ids, res_map),
        )
        summary = {"ok": sres["ok"], "attempts": sres["attempts"], "takeaways": []}
        if sres["ok"]:
            summary["takeaways"] = [
                {
                    **t,
                    "material": [],
                    "parts": R.show([{**t, "material": []}], texts_all)[0]["parts"],
                }
                for t in sres["result"]["takeaways"]
            ]

    calls = (
        sum(len(r["attempts"]) for r in sec_results)
        + len(combine_res["attempts"])
        + len(summary["attempts"])
    )
    return {
        "goal": goal,
        "summary": summary,
        "document": document_out,
        "document_ok": combine_res["ok"],
        "document_attempts": combine_res["attempts"],
        "sections": sections_out,
        "unreviewed_sections": unreviewed,
        "skipped_document_questions": skipped_b,
        "research": research,
        "stats": {
            "sections": len(sections),
            "model_calls": calls + 1,
            "seconds": round(time.time() - t0, 1),
            "research_sections": len(research),
        },
    }
