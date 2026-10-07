"""Local dev server: the teacher Test page and the Sources (ingest/manage) page.

    python scripts/dev_server.py            then open http://localhost:8080

Runs only on this PC (127.0.0.1), against the Docker services (docker compose up -d).
There is no login: never expose this port. Calls to Bedrock cost money (embedding a
question is a tiny fraction of a cent; an answer is about half a cent with Nova Pro).
"""

import json
import os
import re
import sys
import threading
import uuid
from datetime import date
from pathlib import Path
from typing import Annotated

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("ANSWER_MODEL_ID", "amazon.nova-pro-v1:0")

import uvicorn  # noqa: E402
from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from read import pipeline, review, service  # noqa: E402
from read.chunk import sha  # noqa: E402
from read.extract import Unsupported, extract  # noqa: E402
from read.ingest import LICENSE_FIELDS, check_meta  # noqa: E402
from read.retrieve import parse_band  # noqa: E402
from read.store import TempStore, ensure_index  # noqa: E402

CORPUS = ROOT / "corpus"
WEB = ROOT / "web"
MAX_UPLOAD = 60 * 1024 * 1024
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

app = FastAPI(title="R.E.A.D. dev server", docs_url=None, redoc_url=None)
STORES = service.Stores.local(ROOT)
ensure_index(STORES.os)
JOBS: dict[str, dict] = {}
CHECKLIST = ROOT / "rubric" / "checklist.json"
TEMP = TempStore(ROOT / "data" / "plans-temp", hours=24)  # rule 7: deleted after 24 hours
TEMP.purge()
REVIEWS: dict[str, dict] = {}


def meta_path(filename: str) -> Path:
    return CORPUS / f"{filename}.meta.json"


def read_meta(row: dict) -> dict:
    p = meta_path(row.get("source_key", ""))
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_meta(filename: str, meta: dict) -> None:
    meta_path(filename).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )


def get_row(work_id: str) -> dict:
    row = STORES.works.get_item(Key={"work_id": work_id}).get("Item")
    if not row:
        raise HTTPException(404, f"no such source: {work_id}")
    return row


# ---------------- pages ----------------


@app.get("/")
def page_test():
    return FileResponse(WEB / "index.html")


@app.get("/sources")
def page_sources():
    return FileResponse(WEB / "sources.html")


@app.get("/review")
def page_review():
    return FileResponse(WEB / "review.html")


@app.get("/checklist")
def page_checklist():
    return FileResponse(WEB / "checklist.html")


@app.get("/style.css")
def style():
    return FileResponse(WEB / "style.css")


# ---------------- teacher API (same logic the Lambdas will run) ----------------


class SearchIn(BaseModel):
    query: str
    debug: bool = False
    grade_min: int | None = None
    grade_max: int | None = None
    max_results: int = 8
    min_score: float = 0.0
    candidates: int = 40
    use_reranker: bool = True
    keyword_weight: float = 0.3


class AnswerIn(BaseModel):
    query: str
    refs: list[dict]
    debug: bool = False


@app.post("/api/search")
def api_search(body: SearchIn):
    opts = service.SearchOptions(
        **body.model_dump(exclude={"query", "debug"}),
    )
    return service.search(body.query, STORES, debug=body.debug, options=opts)


@app.post("/api/answer")
def api_answer(body: AnswerIn):
    return service.answer(body.query, body.refs, STORES, debug=body.debug)


# ---------------- sources API ----------------


@app.get("/api/sources")
def api_sources():
    works = service.load_works(STORES)
    out = []
    for w in sorted(works.values(), key=lambda w: w["work_id"]):
        meta = read_meta(w)
        out.append(
            {
                "work_id": w["work_id"],
                "title": w.get("title"),
                "publisher": w.get("publisher"),
                "pub_date": w.get("pub_date"),
                "doc_type": w.get("doc_type"),
                "status": service.display_status(w, STORES.today),
                "passages": int(w.get("passage_count", 0)),
                "source_key": w.get("source_key"),
                "version": w.get("source_version_id"),
                "active_version": w.get("active_version_id"),
                "activated_at": w.get("activated_at"),
                "expires_on": w.get("expires_on"),
                "superseded_by": w.get("superseded_by"),
                "placeholder": bool(meta.get("placeholder")),
                "license": meta.get("license") or w.get("license"),
                "license_verified_by": w.get("license_verified_by"),
                "license_verified_on": w.get("license_verified_on"),
                "license_evidence": meta.get("license_evidence", []),
                "license_recommendation": meta.get("license_recommendation"),
                "url": w.get("url"),
            }
        )
    return {"sources": out, "content_last_updated": service.last_updated(works)}


@app.get("/api/sources/{work_id}/sections")
def api_sections(work_id: str, offset: int = 0, limit: int = 50):
    row = get_row(work_id)
    ver = row.get("source_version_id")
    items, kwargs = (
        [],
        {"FilterExpression": "version_id = :v", "ExpressionAttributeValues": {":v": ver}},
    )
    while True:
        page = STORES.sections.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    items.sort(key=lambda s: s["section_id"])
    text = STORES.text.get(work_id, ver)
    out = []
    for s in items[offset : offset + limit]:
        body = text[int(s["char_start"]) : int(s["char_end"])]
        out.append(
            {
                "section_id": s["section_id"],
                "section_path": s.get("section_path"),
                "page": int(s["page"]) if s.get("page") else None,
                "page_end": int(s["page_end"]) if s.get("page_end") else None,
                "words": len(body.split()),
                "text": body,
                "hash_ok": sha(body) == s["text_sha256"],
            }
        )
    return {"total": len(items), "offset": offset, "sections": out}


def run_job(job_id: str, data: bytes, filename: str, meta: dict) -> None:
    job = JOBS[job_id]
    try:
        st = service.Stores.local(ROOT)  # own clients per thread
        job["result"] = pipeline.ingest_file(data, filename, meta, st, job["log"].append)
        job["state"] = "done"
    except Exception as e:  # report every failure to the page
        job["log"].append(f"failed: {e}")
        job["state"] = "failed"


@app.post("/api/sources")
async def api_upload(file: Annotated[UploadFile, File()], meta: Annotated[str, Form()]):
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file over 60 MB")
    try:
        m = json.loads(meta)
    except ValueError:
        raise HTTPException(400, "meta is not valid JSON") from None
    m = {k: v for k, v in m.items() if v not in (None, "", [])}
    problems = check_meta(m)
    if problems:
        raise HTTPException(400, "; ".join(problems))
    filename = SAFE_NAME.sub("-", Path(file.filename or "upload").name).strip("-") or "upload"
    CORPUS.mkdir(exist_ok=True)
    (CORPUS / filename).write_bytes(data)
    for f in LICENSE_FIELDS:
        m.setdefault(f, "")
    write_meta(filename, m)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "state": "running",
        "log": [f"saved {filename} ({len(data):,} bytes)"],
        "result": None,
    }
    threading.Thread(target=run_job, args=(job_id, data, filename, m), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "no such job")
    return JOBS[job_id]


class ApproveIn(BaseModel):
    name: str
    license: str


@app.post("/api/sources/{work_id}/approve")
def api_approve(work_id: str, body: ApproveIn):
    """A person signs off the license (rule 6): records their name and today's date."""
    name, lic = body.name.strip(), body.license.strip()
    if len(name) < 3 or not lic:
        raise HTTPException(400, "type your full name and the license")
    row = get_row(work_id)
    meta = read_meta(row) or {k: v for k, v in row.items()}
    meta.update(license=lic, license_verified_by=name, license_verified_on=date.today().isoformat())
    write_meta(row["source_key"], meta)
    try:
        return pipeline.activate(work_id, meta, STORES, lambda m: None)
    except pipeline.IngestError as e:
        raise HTTPException(400, str(e)) from None


class StatusIn(BaseModel):
    active: bool


@app.post("/api/sources/{work_id}/status")
def api_status(work_id: str, body: StatusIn):
    try:
        pipeline.set_status(work_id, "ready" if body.active else "inactive", STORES)
    except pipeline.IngestError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


class TagsIn(BaseModel):
    expires_on: str | None = None
    superseded_by: str | None = None


@app.patch("/api/sources/{work_id}")
def api_tags(work_id: str, body: TagsIn):
    row = get_row(work_id)
    changes = body.model_dump()
    if changes["superseded_by"] and changes["superseded_by"] == work_id:
        raise HTTPException(400, "a source can't supersede itself")
    try:
        pipeline.update_tags(work_id, changes, STORES)
    except pipeline.IngestError as e:
        raise HTTPException(400, str(e)) from None
    meta = read_meta(row)
    if meta:
        for k, v in changes.items():
            if v:
                meta[k] = v
            else:
                meta.pop(k, None)
        write_meta(row["source_key"], meta)
    return {"ok": True}


@app.delete("/api/sources/{work_id}")
def api_delete(work_id: str, confirm: str = ""):
    if confirm != work_id:
        raise HTTPException(400, "confirm must equal the work_id")
    try:
        pipeline.delete_work(work_id, STORES)
    except pipeline.IngestError as e:
        raise HTTPException(400, str(e)) from None
    return {
        "ok": True,
        "note": "Removed from search and tables; the file and meta.json stay in corpus/.",
    }


# ---------------- material review (development checklist: rubric/checklist.json) ----------------


@app.get("/api/checklist")
def api_checklist():
    return review.load_checklist(CHECKLIST)


@app.put("/api/checklist")
def api_checklist_save(body: dict):
    problems = review.check_checklist(body)
    if problems:
        raise HTTPException(400, "; ".join(problems[:5]))
    current = review.load_checklist(CHECKLIST)
    keep = {k: current[k] for k in ("status", "note", "owner") if k in current}
    CHECKLIST.write_text(
        json.dumps({**keep, **body, **keep}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"ok": True}


@app.post("/api/review")
async def api_review_upload(
    synthetic: Annotated[bool, Form()],
    goal_note: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
):
    """Upload sample material (file or pasted text); returns the inferred goal to confirm."""
    if not synthetic:
        raise HTTPException(
            400, "Use sample (synthetic) material only: no real student or teacher data."
        )
    TEMP.purge()
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "file over 60 MB")
        try:
            ex = extract(data)
        except Unsupported as e:
            raise HTTPException(400, f"Can't read that file: {e}") from None
        kind, material = ex.kind, ex.text
    elif text.strip():
        kind, material = "pasted text", text.replace("\r\n", "\n").replace("\r", "\n").strip()
    else:
        raise HTTPException(400, "Upload a file or paste the material.")
    words = len(material.split())
    if words > review.MAX_MATERIAL_WORDS:
        raise HTTPException(
            400, f"Material is {words:,} words; the limit is {review.MAX_MATERIAL_WORDS:,}."
        )
    parts = review.material_parts(material)
    if not parts:
        raise HTTPException(400, "No readable text found in the material.")
    rid = TEMP.new()
    TEMP.put_json(rid, "material.json", {"kind": kind, "text": material, "parts": parts})
    try:
        goal = review.infer_goal(parts, goal_note, STORES.bedrock)
    except Exception as e:  # show the problem; the teacher can still type the goal
        goal = review.clean_goal({"objective": goal_note, "grade_band": "K-5"})
        goal["inference_error"] = str(e)[:200]
    TEMP.put_json(rid, "goal.json", goal)
    return {"review_id": rid, "kind": kind, "words": words, "parts": len(parts), "goal": goal}


class GoalIn(BaseModel):
    material_type: str
    grade_band: str
    focus: str
    objective: str


def _evidence_for(st, criterion: dict, goal: dict) -> tuple[list[dict], list[dict]]:
    band = parse_band(goal["grade_band"]) or (None, None)
    opts = service.SearchOptions(grade_min=band[0], grade_max=band[1], max_results=4)
    res = service.search(f"{criterion['search_query']}. {goal['objective']}", st, options=opts)
    excerpts = res["excerpts"]
    return [{"cite_id": x["cite_id"], "text": x["text"]} for x in excerpts], excerpts


def run_review(rid: str, goal: dict, criteria: list[dict]) -> None:
    from concurrent.futures import ThreadPoolExecutor

    state = REVIEWS[rid]
    material = TEMP.get_json(rid, "material.json")
    parts = material["parts"]
    local = threading.local()

    def one(criterion: dict) -> dict:
        if not hasattr(local, "st"):
            local.st = service.Stores.local(ROOT)
        st = local.st
        base = {k: criterion[k] for k in ("criterion_id", "component", "question")}
        try:
            evidence, excerpts = _evidence_for(st, criterion, goal)
            res = review.review_criterion(
                criterion,
                goal,
                parts,
                evidence,
                lambda c, g, p, e, fb: review.call_review(c, g, p, e, st.bedrock, fb),
            )
        except Exception as e:  # one failed question shouldn't stop the review
            res, excerpts = (
                {"ok": False, "problems": [f"{type(e).__name__}: {e}"[:200]], "attempts": []},
                [],
            )
        out = {**base, "ok": res["ok"], "attempts": res["attempts"], "research": excerpts}
        if res["ok"]:
            out["review"] = review.display(
                res["result"],
                parts,
                [{"cite_id": x["cite_id"], "text": x["text"]} for x in excerpts],
            )
        else:
            out["problems"] = res["problems"]
        state["results"].append(out)
        return out

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, criteria))
    order = {c["criterion_id"]: i for i, c in enumerate(criteria)}
    state["results"].sort(key=lambda r: order[r["criterion_id"]])
    state["state"] = "done"
    TEMP.put_json(rid, "review.json", {"goal": goal, "results": state["results"]})


@app.post("/api/review/{rid}/run")
def api_review_run(rid: str, body: GoalIn):
    try:
        TEMP.path(rid, "material.json")
    except KeyError:
        raise HTTPException(404, "review not found (material is deleted after 24 hours)") from None
    goal = review.clean_goal(body.model_dump())
    TEMP.put_json(rid, "goal.json", goal)
    checklist = review.load_checklist(CHECKLIST)
    criteria = review.applicable(checklist["criteria"], goal["grade_band"])
    REVIEWS[rid] = {"state": "running", "total": len(criteria), "results": [], "goal": goal}
    threading.Thread(target=run_review, args=(rid, goal, criteria), daemon=True).start()
    return {"total": len(criteria)}


@app.get("/api/review/{rid}")
def api_review_status(rid: str):
    try:
        material = TEMP.get_json(rid, "material.json")
    except (KeyError, FileNotFoundError):
        raise HTTPException(404, "review not found (material is deleted after 24 hours)") from None
    state = REVIEWS.get(rid, {"state": "not started", "total": 0, "results": []})
    checklist = review.load_checklist(CHECKLIST)
    return {
        **state,
        "done": len(state["results"]),
        "material": {"kind": material["kind"], "parts": material["parts"]},
        "checklist_status": checklist.get("status"),
        "checklist_note": checklist.get("note"),
        "disclaimer": service.DISCLAIMER,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
