"""The R.E.A.D. web app: pages plus the JSON API (D90).

One FastAPI app, run two ways:
  - in AWS by the API Lambda (src/handlers/api.py, through Mangum), behind API Gateway and the
    passcode authorizer;
  - on this PC by scripts/dev_server.py, against the same AWS stack (no Docker).
Long work (ingest, review) goes to read.jobs; its state lives in S3 so any Lambda can poll it.
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from read import pipeline, review, service
from read.chunk import sha
from read.extract import Unsupported, extract
from read.ingest import LICENSE_FIELDS, check_meta
from read.jobs import CHECKLIST_KEY, WEB, Env

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
NO_CACHE = {"Cache-Control": "no-cache"}  # pages and styles always revalidate
PAGES = {
    "/": "index.html",
    "/sources": "sources.html",
    "/review": "review.html",
    "/checklist": "checklist.html",
    "/style.css": "style.css",
    "/auth.js": "auth.js",
}
GONE = "review not found (material is deleted after 24 hours)"


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


class ApproveIn(BaseModel):
    name: str
    license: str


class StatusIn(BaseModel):
    active: bool


class TagsIn(BaseModel):
    expires_on: str | None = None
    superseded_by: str | None = None


class GoalIn(BaseModel):
    material_type: str
    grade_band: str
    focus: str
    objective: str
    doc_questions: list[str] = []  # whole-document question ids the teacher kept on


def create_app(env: Env) -> FastAPI:
    app = FastAPI(title="R.E.A.D.", docs_url=None, redoc_url=None, openapi_url=None)
    cache: dict = {}

    def st() -> service.Stores:  # created on first use, reused while the Lambda stays warm
        if "st" not in cache:
            cache["st"] = env.stores()
        return cache["st"]

    def read_meta(row: dict) -> dict:
        return env.corpus.get_json(f"{row.get('source_key', '')}.meta.json") or {}

    def write_meta(filename: str, meta: dict) -> None:
        env.corpus.put_json(f"{filename}.meta.json", meta)

    def get_row(work_id: str) -> dict:
        row = st().works.get_item(Key={"work_id": work_id}).get("Item")
        if not row:
            raise HTTPException(404, f"no such source: {work_id}")
        return row

    def too_big() -> HTTPException:
        mb = env.max_upload / (1024 * 1024)
        return HTTPException(
            413, f"file over {mb:g} MB; ingest large sources with scripts/ingest.py"
        )

    # ---------------- pages ----------------

    for route, name in PAGES.items():

        def page(name: str = name):
            return FileResponse(WEB / name, headers=NO_CACHE)

        app.add_api_route(route, page, methods=["GET"], include_in_schema=False)

    # ---------------- teacher API ----------------

    @app.get("/api/ping")
    def api_ping():
        """Cheap passcode check: pages call it on load so the passcode box shows up front."""
        return {"ok": True}

    @app.post("/api/search")
    def api_search(body: SearchIn):
        opts = service.SearchOptions(**body.model_dump(exclude={"query", "debug"}))
        return service.search(body.query, st(), debug=body.debug, options=opts)

    @app.post("/api/answer")
    def api_answer(body: AnswerIn):
        return service.answer(body.query, body.refs, st(), debug=body.debug)

    # ---------------- sources API ----------------

    @app.get("/api/sources")
    def api_sources():
        works = service.load_works(st())
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
                    "status": service.display_status(w, st().today),
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
            page = st().sections.scan(**kwargs)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        items.sort(key=lambda s: s["section_id"])
        text = st().text.get(work_id, ver)
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

    @app.post("/api/sources")
    async def api_upload(file: Annotated[UploadFile, File()], meta: Annotated[str, Form()]):
        data = await file.read()
        if len(data) > env.max_upload:
            raise too_big()
        try:
            m = json.loads(meta)
        except ValueError:
            raise HTTPException(400, "meta is not valid JSON") from None
        m = {k: v for k, v in m.items() if v not in (None, "", [])}
        problems = check_meta(m)
        if problems:
            raise HTTPException(400, "; ".join(problems))
        filename = SAFE_NAME.sub("-", Path(file.filename or "upload").name).strip("-") or "upload"
        env.corpus.put(filename, data)
        for f in LICENSE_FIELDS:
            m.setdefault(f, "")
        write_meta(filename, m)
        job_id = env.temp.new()
        env.temp.put_json(
            job_id,
            "job.json",
            {
                "state": "running",
                "log": [f"saved {filename} ({len(data):,} bytes)"],
                "result": None,
            },
        )
        env.start({"kind": "ingest", "job_id": job_id, "filename": filename})
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str):
        try:
            return env.temp.get_json(job_id, "job.json")
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "no such job") from None

    @app.post("/api/sources/{work_id}/approve")
    def api_approve(work_id: str, body: ApproveIn):
        """A person signs off the license (rule 6): records their name and today's date."""
        name, lic = body.name.strip(), body.license.strip()
        if len(name) < 3 or not lic:
            raise HTTPException(400, "type your full name and the license")
        row = get_row(work_id)
        meta = read_meta(row) or dict(row)
        meta.update(
            license=lic, license_verified_by=name, license_verified_on=date.today().isoformat()
        )
        write_meta(row["source_key"], meta)
        try:
            return pipeline.activate(work_id, meta, st(), lambda m: None)
        except pipeline.IngestError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/sources/{work_id}/status")
    def api_status(work_id: str, body: StatusIn):
        try:
            pipeline.set_status(work_id, "ready" if body.active else "inactive", st())
        except pipeline.IngestError as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True}

    @app.patch("/api/sources/{work_id}")
    def api_tags(work_id: str, body: TagsIn):
        row = get_row(work_id)
        changes = body.model_dump()
        if changes["superseded_by"] and changes["superseded_by"] == work_id:
            raise HTTPException(400, "a source can't supersede itself")
        try:
            pipeline.update_tags(work_id, changes, st())
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
            pipeline.delete_work(work_id, st())
        except pipeline.IngestError as e:
            raise HTTPException(400, str(e)) from None
        return {
            "ok": True,
            "note": "Removed from search and tables; the file and meta.json stay in the "
            "sources bucket.",
        }

    # ---------- material review (development checklist: rubric/checklist.json) ----------

    @app.get("/api/checklist")
    def api_checklist():
        return env.checklist()

    @app.put("/api/checklist")
    def api_checklist_save(body: dict):
        problems = review.check_checklist(body)
        if problems:
            raise HTTPException(400, "; ".join(problems[:5]))
        current = env.checklist()
        keep = {k: current[k] for k in ("status", "note", "owner") if k in current}
        env.app_files.put_json(CHECKLIST_KEY, {**keep, **body, **keep})
        return {"ok": True}

    @app.post("/api/review")
    async def api_review_upload(
        synthetic: Annotated[bool, Form()],
        goal_note: Annotated[str, Form()] = "",
        text: Annotated[str, Form()] = "",
        file: Annotated[UploadFile | None, File()] = None,
    ):
        """Upload sample material (file or pasted text); returns the inferred goal and the
        whole-document questions judged relevant, for the teacher to confirm."""
        if not synthetic:
            raise HTTPException(
                400, "Use sample (synthetic) material only: no real student or teacher data."
            )
        env.temp.purge()
        headings: set[str] = set()
        levels: dict[str, int] = {}
        if file is not None and file.filename:
            data = await file.read()
            if len(data) > env.max_upload:
                raise too_big()
            try:
                ex = extract(data)
            except Unsupported as e:
                raise HTTPException(400, f"Can't read that file: {e}") from None
            kind, material, headings, levels = ex.kind, ex.text, set(ex.headings), ex.heading_levels
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
        sections = review.make_sections(parts, headings, levels)
        rid = env.temp.new()
        env.temp.put_json(
            rid,
            "material.json",
            {"kind": kind, "text": material, "parts": parts, "sections": sections},
        )
        _, list_b = review.split_checklist(env.checklist()["criteria"], "K-12")
        try:
            goal = review.infer_goal(parts, sections, goal_note, list_b, st().bedrock)
        except Exception as e:  # show the problem; the teacher can still type the goal
            goal = review.clean_goal({"objective": goal_note, "grade_band": "K-5"}, list_b)
            goal["inference_error"] = str(e)[:200]
        env.temp.put_json(rid, "goal.json", goal)
        return {
            "review_id": rid,
            "kind": kind,
            "words": words,
            "parts": len(parts),
            "sections": [{"title": x["title"], "parts": len(x["parts"])} for x in sections],
            "goal": goal,
        }

    @app.post("/api/review/{rid}/run")
    def api_review_run(rid: str, body: GoalIn):
        try:
            if not env.temp.exists(rid, "material.json"):
                raise FileNotFoundError(rid)
            inferred = env.temp.get_json(rid, "goal.json")
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, GONE) from None
        goal = review.clean_goal(body.model_dump())
        goal["doc_questions"] = inferred.get("doc_questions", [])
        env.temp.put_json(rid, "goal.json", goal)
        env.temp.put_json(
            rid, "state.json", {"state": "running", "phase": "Starting", "done": 0, "total": 0}
        )
        env.start({"kind": "review", "rid": rid, "chosen_b": sorted(body.doc_questions)})
        return {"ok": True}

    @app.get("/api/review/{rid}")
    def api_review_status(rid: str):
        try:
            material = env.temp.get_json(rid, "material.json")
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, GONE) from None
        try:
            state = env.temp.get_json(rid, "state.json")
        except FileNotFoundError:
            state = {"state": "not started"}
        if state.get("state") == "done":
            try:
                state["result"] = env.temp.get_json(rid, "review.json")
            except FileNotFoundError:
                state = {"state": "failed", "error": "the finished review was not saved"}
        checklist = env.checklist()
        return {
            **state,
            "material": {"kind": material["kind"], "parts": material["parts"]},
            "checklist_status": checklist.get("status"),
            "checklist_note": checklist.get("note"),
            "disclaimer": service.DISCLAIMER,
        }

    return app
