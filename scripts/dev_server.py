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

from read import pipeline, service  # noqa: E402
from read.chunk import sha  # noqa: E402
from read.ingest import LICENSE_FIELDS, check_meta  # noqa: E402
from read.store import ensure_index  # noqa: E402

CORPUS = ROOT / "corpus"
WEB = ROOT / "web"
MAX_UPLOAD = 60 * 1024 * 1024
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

app = FastAPI(title="R.E.A.D. dev server", docs_url=None, redoc_url=None)
STORES = service.Stores.local(ROOT)
ensure_index(STORES.os)
JOBS: dict[str, dict] = {}


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


@app.get("/style.css")
def style():
    return FileResponse(WEB / "style.css")


# ---------------- teacher API (same logic the Lambdas will run) ----------------


class SearchIn(BaseModel):
    query: str
    debug: bool = False


class AnswerIn(BaseModel):
    query: str
    refs: list[dict]
    debug: bool = False


@app.post("/api/search")
def api_search(body: SearchIn):
    return service.search(body.query, STORES, debug=body.debug)


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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
