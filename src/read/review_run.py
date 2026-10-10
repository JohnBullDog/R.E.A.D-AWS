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
SUMMARY_ON = False  # the page's at-a-glance replaces the takeaways summary (D88)
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
            "explicit": q.get("evidence") == "explicit",  # presence question (D79)
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
    q_text = {q["criterion_id"]: q["question"] for q in list_a} | {
        q["id"]: q["question"] for q in qb
    }

    # 3. triage (off by default: every section question is offered to every section)
    if R.TRIAGE_ON and list_a:
        progress(phase="Matching questions to sections", done=0, total=len(sections))
        for q in list_a:
            key = q["question"] + " " + q["search_query"]
            if key not in _QUESTION_VECS:
                _QUESTION_VECS[key] = deps["embed"](key)
        qvecs = {
            q["criterion_id"]: _QUESTION_VECS[q["question"] + " " + q["search_query"]]
            for q in list_a
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
        tri = [R.triage(v, qvecs) for v in svecs]
    else:
        tri = [
            {q["criterion_id"]: {"label": "maybe", "similarity": None} for q in list_a}
            for _ in sections
        ]
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
    mat_texts = {p["id"]: p["text"] for p in parts}  # + a call's own research: its copy check

    def research_for(qids, per_question=None):
        """S ids for these questions (best-ranked first per question), in S order."""
        ids = {s for q in qids for s in q_research.get(q, [])[:per_question]}
        return sorted(ids, key=lambda s: int(s[1:]))

    # 5. one call per section question (D84): asked all at once with their research, Nova
    # sometimes answers only one of them (like the whole-document verdicts, D66)
    jobs = [(k, qid) for k in range(len(sections)) for qid in offered[k]]
    progress(phase="Reviewing sections", done=0, total=len(jobs))
    done = {"n": 0}

    def review_question(job):
        k, qid = job
        sec = sections[k]
        sec_research = research_for([qid], R.SECTION_RESEARCH)
        items = [{"cite_id": s, "text": research[s]["text"]} for s in sec_research]
        labels = {qid: tri[k][qid]["label"]}
        prompt = R.section_prompt(goal, sections, sec, parts_by_id, [a_by_id[qid]], labels, items)
        res_map = {s: research[s]["text"] for s in sec_research}
        fixes: list[str] = []

        def ask(fb):
            raw = R.converse(
                deps["client"],
                R.SECTION_SYSTEM,
                prompt + (f"\n\n{fb}" if fb else ""),
                "record_section",
                R.SECTION_SCHEMA,
                1200,
            )
            clean, notes = R.sanitize_section(raw, {qid}, sec, res_map, mat_texts | res_map, labels)
            fixes[:] = notes
            return clean

        try:
            out = R.with_retry(
                ask,
                lambda o: R.validate_section(o, sec, {qid}, mat_texts | res_map, res_map, q_text),
                lambda o: R.drop_restated(o, q_text, res_map),
            )
        except Exception as exc:  # one broken question must not stop the review
            out = {"ok": False, "problems": [f"{type(exc).__name__}: {exc}"[:300]], "attempts": []}
        out["fixes"] = list(fixes) + out.get("salvaged", [])
        done["n"] += 1
        progress(phase="Reviewing sections", done=done["n"], total=len(jobs))
        return k, qid, out

    with ThreadPoolExecutor(WORKERS) as pool:
        by_q = list(pool.map(review_question, jobs))
    sec_results = []
    for k in range(len(sections)):
        mine = [(qid, r) for kk, qid, r in by_q if kk == k]
        good = [r for _, r in mine if r["ok"]]
        sec_results.append(
            {
                "ok": bool(good) or not mine,
                "result": {"findings": [f for r in good for f in r["result"]["findings"]]},
                "attempts": [a for _, r in mine for a in r["attempts"]],
                "fixes": [x for _, r in mine for x in r["fixes"]]
                + [f"{qid}: feedback couldn't be generated" for qid, r in mine if not r["ok"]],
                "problems": [p for _, r in mine if not r["ok"] for p in r.get("problems", [])],
            }
        )

    findings, sections_out = [], []
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
        else:
            entry["problems"] = res["problems"]
        sections_out.append(entry)
    unreviewed = [s["title"] for s, r in zip(sections, sec_results, strict=True) if not r["ok"]]

    # 6. whole-document pass: one call per question over the whole material when it fits;
    # otherwise each chunk answers, then a merge call per question writes the verdict
    chunks = R.doc_chunks(parts, sections, R.DOC_CHUNK_WORDS)
    single = len(chunks) == 1
    per_question = {
        q["id"]: {
            "status": None,
            "answers": [],
            "single": single,
            "cited": [],
            "explicit": q["explicit"],
        }
        for q in qb
    }
    verdicts, document_out = [], []
    trail_attempts: list[dict] = []
    combine_res = {"ok": True, "attempts": []}
    part_attempts: list[dict] = []
    mats = {p["id"] for p in parts}
    all_parts = {p["id"]: p["text"] for p in parts}
    if qb and not single:
        jobs = [(q, c) for q in qb for c in chunks]
        progress(phase="Reading the material in parts", done=0, total=len(jobs))
        pdone = {"n": 0}

        def answer_part(job):
            q, c = job
            prompt = R.part_prompt(goal, sections, c, [parts_by_id[m] for m in c["parts"]], q)
            try:
                res = R.with_retry(
                    lambda fb: R.converse(
                        deps["client"],
                        R.PART_SYSTEM,
                        prompt + (f"\n\n{fb}" if fb else ""),
                        "record_part",
                        R.PART_SCHEMA,
                        500,
                    ),
                    lambda o: R.validate_part(o, c),
                    R.trim_part,
                )
            except Exception as exc:
                res = {"ok": False, "problems": [f"{type(exc).__name__}: {exc}"[:300]]}
                res["attempts"] = []
            pdone["n"] += 1
            progress(phase="Reading the material in parts", done=pdone["n"], total=len(jobs))
            return q, c, res

        with ThreadPoolExecutor(WORKERS) as pool:
            for q, c, res in pool.map(answer_part, jobs):
                part_attempts += res["attempts"]
                r = res.get("result") if res["ok"] else None
                per_question[q["id"]]["answers"].append(
                    {
                        "section": c["title"],
                        "answer": r["answer"] if r else R.UNANSWERED,
                        "material": list(r["material"]) if r else [],
                        "note": " ".join(str(r["note"]).split())[:300] if r else "not answered",
                    }
                )
        for q in qb:
            info = per_question[q["id"]]
            info["status"] = R.combine_status(q["rule"], [a["answer"] for a in info["answers"]])
            info["cited"] = sorted(
                {m for a in info["answers"] for m in a["material"]}, key=lambda m: int(m[1:])
            )
    if qb:
        # evidence before verdicts: presence questions find passages and confirm each one (D81);
        # judgment questions gather passages for and against and grade each one (D98)
        def confirm(q):
            try:
                if q["explicit"]:
                    found = [
                        m
                        for c in chunks
                        for m in R.find_evidence(
                            deps["client"], q["question"], [parts_by_id[i] for i in c["parts"]]
                        )
                    ]
                    return q["id"], R.verify_parts(
                        deps["client"], q["question"], parts_by_id, found
                    )
                context = R.goal_for(goal, q)
                found = [
                    m
                    for c in chunks
                    for m in R.gather_evidence(
                        deps["client"], context, q["question"], [parts_by_id[i] for i in c["parts"]]
                    )
                ]
                return q["id"], R.grade_stance(
                    deps["client"], context, q["question"], parts_by_id, found
                )
            except Exception:  # unknown: fall back to the verdict call's own judgment
                return q["id"], None

        progress(phase="Finding evidence", done=0, total=len(qb))
        with ThreadPoolExecutor(WORKERS) as pool:
            for qid, graded in pool.map(confirm, qb):
                info = per_question[qid]
                info["graded"] = graded
                if graded is None:
                    continue
                if info["explicit"]:
                    info["confirmed"] = list(graded)
                    info["full"] = [m for m, g in graded.items() if g == "full"]
                else:  # strong/some count as confirmed; met needs a strong one, no 'against'
                    info["confirmed"] = [m for m, g in graded.items() if g in ("strong", "some")]
                    info["strong"] = [m for m, g in graded.items() if g == "strong"]
                    info["against"] = [m for m, g in graded.items() if g == "against"]
                    info["full"] = [] if info["against"] else list(info["strong"])
        qb = [
            {
                **q,
                "confirmed": per_question[q["id"]].get("confirmed"),
                "full": per_question[q["id"]].get("full"),
                "against": per_question[q["id"]].get("against"),
                "strong": per_question[q["id"]].get("strong"),
            }
            for q in qb
        ]
        progress(phase="Checking the whole document", done=0, total=len(qb))

        def verdict_one(q):  # one small call per question: Nova won't fill many entries at once
            q_res = research_for([q["id"]])
            items = [{"cite_id": s, "text": research[s]["text"]} for s in q_res]
            res_map = {s: research[s]["text"] for s in q_res}
            if single:
                system = R.DOC_SYSTEM
                prompt = R.doc_prompt(goal, sections, parts, q, items)
            else:
                system = R.COMBINE_SYSTEM
                cited = [parts_by_id[m] for m in per_question[q["id"]]["cited"]]
                prompt = R.combine_prompt(goal, [q], per_question, items, cited)
            confirmed = per_question[q["id"]].get("confirmed")

            def ask(fb):
                out = R.sanitize_combine(
                    R.converse(
                        deps["client"],
                        system,
                        prompt + (f"\n\n{fb}" if fb else ""),
                        "record_verdicts",
                        R.COMBINE_SCHEMA,
                        1200,
                    ),
                    res_map,
                    all_parts,
                    q["id"],
                )
                return R.set_confirmed(out, confirmed, per_question[q["id"]].get("full"))

            def check(o):
                return R.validate_combine(
                    o, {q["id"]}, per_question, mats, mat_texts | res_map, res_map, q_text
                )

            def salvage(o):
                return R.salvage_verdicts(o, all_parts, res_map)

            try:
                res = R.with_retry(ask, check, salvage)
            except Exception as exc:
                return {
                    "ok": False,
                    "problems": [f"{type(exc).__name__}: {exc}"[:300]],
                    "attempts": [],
                }
            if not res["ok"]:
                return res
            first = next(iter(res["result"].get("verdicts") or []), {}).get("verdict")
            graded = per_question[q["id"]].get("graded")
            if not graded:  # nothing to read: the rule already made it missing, or no evidence step
                res["second_opinion"] = {
                    "status": "not needed" if graded == {} else "unavailable",
                    "first": first,
                    "final": first,
                }
                return res
            blind = R.blind_verdict(
                deps["client"],
                R.blind_prompt(goal, q, [parts_by_id[m] for m in graded], graded, items),
            )
            if blind is None:
                res["second_opinion"] = {"status": "unavailable", "first": first, "final": first}
                return res
            opinion = {**blind, "first": first, "final": first, "status": "agreed"}
            if blind["verdict"] != first:
                # D102: disagreement lowers confidence, never the bar. The more cautious reading
                # wins: a lower independent reading re-decides the verdict down to it (still
                # validated against the evidence rules); a higher one leaves it as it was.
                opinion["status"] = "uncertain"
                if R.VERDICTS.index(blind["verdict"]) > R.VERDICTS.index(first):
                    look = R.second_look(blind)
                    try:
                        again = R.with_retry(
                            lambda fb: ask(f"{look}\n\n{fb}" if fb else look), check, salvage
                        )
                    except Exception:
                        again = {"ok": False, "attempts": []}
                    res["attempts"] += again.get("attempts", [])
                    final = (
                        next(iter(again["result"].get("verdicts") or []), {}).get("verdict")
                        if again["ok"]
                        else None
                    )
                    if final == blind["verdict"]:  # only accept the cautious verdict itself
                        res = {**again, "attempts": res["attempts"]}
                        opinion["final"] = final
            res["second_opinion"] = opinion
            return res

        with ThreadPoolExecutor(WORKERS) as pool:
            combined = list(pool.map(verdict_one, qb))
        combine_res = {
            "ok": all(c["ok"] for c in combined),
            "attempts": part_attempts + [a for c in combined for a in c["attempts"]],
        }
        got = {}
        for c in combined:
            for v in c.get("result", {}).get("verdicts", []) if c["ok"] else []:
                got[v["question_id"]] = v

        # 6c. evidence trail per verdict (D88)
        where_of = {m: sec["title"] for sec in sections for m in sec["owned"]}
        progress(phase="Laying out the evidence", done=0, total=len(got))
        tdone = {"n": 0}

        def trail_one(q):
            v = got[q["id"]]
            cited = [
                m
                for x in v.get("observation") or []
                for m in x.get("material") or []
                if m in parts_by_id
            ]
            ids = list(dict.fromkeys(list(v.get(R.VERIFIED) or []) + cited))
            ids += [m for m in per_question[q["id"]].get("confirmed") or [] if m not in ids]
            ids = ids[:6]
            q_res = research_for([q["id"]])
            items = [{"cite_id": x, "text": research[x]["text"]} for x in q_res]
            res_map = {x: research[x]["text"] for x in q_res}
            prompt = R.trail_prompt(goal, q, v, [parts_by_id[m] for m in ids], items)

            def ask(fb):
                return R.sanitize_trail(
                    R.converse(
                        deps["client"],
                        R.TRAIL_SYSTEM,
                        prompt + (f"\n\n{fb}" if fb else ""),
                        "record_trail",
                        R.TRAIL_SCHEMA,
                        1500,
                    ),
                    v["verdict"],
                    set(ids),
                )

            try:
                res = R.with_retry(
                    ask,
                    lambda o: R.validate_trail(o, mat_texts, res_map),
                    lambda o: R.salvage_trail(o, mat_texts, res_map),
                )
                view = None
                if res["ok"]:
                    view = R.trail_view(
                        R.research_checks(
                            R.more_support(res["result"], res_map, deps["client"]),
                            res_map,
                            deps["client"],
                            q["question"],
                        ),
                        texts_all,
                        where_of,
                    )
            except Exception as exc:
                res = {"ok": False, "problems": [f"{type(exc).__name__}: {exc}"[:300]]}
                res["attempts"], view = [], None
            tdone["n"] += 1
            progress(phase="Laying out the evidence", done=tdone["n"], total=len(got))
            return q["id"], view, res

        with ThreadPoolExecutor(WORKERS) as pool:
            trails = {
                qid: (view, res)
                for qid, view, res in pool.map(trail_one, [q for q in qb if q["id"] in got])
            }
        trail_attempts = [a for _, res in trails.values() for a in res["attempts"]]
        for q in qb:
            v = got.get(q["id"])
            row = {
                "question_id": q["id"],
                "question": q["question"],
                "core": q["core"],
                "component": q["component"],
                "answers": per_question[q["id"]]["answers"],
                "status": per_question[q["id"]]["status"],
                "fixes": combined[qb.index(q)].get("salvaged", []),
                "second_opinion": combined[qb.index(q)].get("second_opinion"),
                "evidence_graded": per_question[q["id"]].get("graded"),
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
                    "evidence_checked": v.get(R.VERIFIED),
                    "evidence_confirmed": per_question[q["id"]].get("confirmed"),
                    "trail": trails.get(q["id"], (None, {}))[0],
                    "trail_fixes": trails.get(q["id"], (None, {}))[1].get("salvaged", []),
                    "trail_problems": trails.get(q["id"], (None, {}))[1].get("problems", []),
                }
            document_out.append(row)

    # 7. summary
    summary = {"ok": False, "takeaways": [], "attempts": []}
    if SUMMARY_ON and (findings or verdicts):
        progress(phase="Writing the summary", done=0, total=1)
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
            R.salvage_summary,
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
        + len(trail_attempts)
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
            "doc_chunks": len(chunks),
        },
    }
