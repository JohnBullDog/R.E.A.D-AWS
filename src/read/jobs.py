"""Long-running work: ingesting a source and running a material review (D90).

On AWS the API Lambda hands these to the worker Lambda (an async invoke, because a review takes
minutes and API Gateway waits at most 30 seconds); the local dev server runs them in a thread.
State lives in the plans-temp bucket so the page can poll it from any Lambda:
  <job_id>/job.json      ingest log, state, result
  <rid>/state.json       review phase and progress; <rid>/review.json the finished result
"""

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _bundled(*names: str) -> Path:
    """A file shipped with the code: next to the package in Lambda, at the repo root locally."""
    for base in (HERE.parent, HERE.parent.parent):
        p = base.joinpath(*names)
        if p.exists():
            return p
    return HERE.parent.parent.joinpath(*names)


WEB = _bundled("web")
DEFAULT_CHECKLIST = _bundled("rubric", "checklist.json")
CHECKLIST_KEY = "checklist.json"
STATE_EVERY = 2.0  # seconds between review progress writes
REVIEW_RESEARCH = (
    6  # research sections per checklist question (was 4; more to back suggestions, D101)
)


def load_env_file(path: Path) -> None:
    """KEY=VALUE lines from .env.aws (scripts/stack_env.py) into os.environ, for local runs."""
    if not path.exists():
        raise SystemExit(f"{path.name} not found: run python scripts/stack_env.py after deploying")
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


@dataclass
class Env:
    """Everything the app and the worker need, for one deployment."""

    stores: Callable[[], object]  # new read.service.Stores (own clients per thread)
    temp: object  # S3TempStore: material, review state and results, job logs (1-day expiry)
    corpus: object  # S3FileStore: uploaded sources and their meta.json sidecars
    app_files: object  # S3FileStore: the edited checklist
    start: Callable[[dict], None]  # run a job in the background
    max_upload: int = 4_000_000  # base64-encoded, it must fit a 6 MB Lambda payload

    @classmethod
    def from_environ(cls, profile: str | None = None, env: dict | None = None) -> "Env":
        import boto3

        from read.service import Stores
        from read.store import S3FileStore, S3TempStore

        env = env if env is not None else dict(os.environ)
        session = boto3.Session(profile_name=profile, region_name="us-east-1")
        s3 = session.client("s3")
        worker = env.get("WORKER_FUNCTION")
        holder: dict = {}

        if worker:  # in AWS: hand the job to the worker Lambda
            lam = session.client("lambda")

            def start(payload: dict) -> None:
                lam.invoke(
                    FunctionName=worker,
                    InvocationType="Event",
                    Payload=json.dumps(payload).encode("utf-8"),
                )
        else:  # local dev server: same code in a thread

            def start(payload: dict) -> None:
                threading.Thread(target=handle, args=(holder["env"], payload), daemon=True).start()

        e = cls(
            stores=lambda: Stores.aws(profile, env),
            temp=S3TempStore(s3, env["TEMP_BUCKET"]),
            corpus=S3FileStore(s3, env["RAW_BUCKET"]),
            app_files=S3FileStore(s3, env["APP_BUCKET"]),
            start=start,
            max_upload=int(env.get("MAX_UPLOAD_BYTES", 4_000_000)),
        )
        holder["env"] = e
        return e

    def checklist(self) -> dict:
        saved = self.app_files.get_json(CHECKLIST_KEY)
        return saved if saved else json.loads(DEFAULT_CHECKLIST.read_text(encoding="utf-8"))


def handle(env: Env, payload: dict) -> None:
    """Run one job described by payload["kind"]."""
    kind = payload.get("kind")
    if kind == "ingest":
        run_ingest(env, payload["job_id"], payload["filename"])
    elif kind == "review":
        run_review(env, payload["rid"], set(payload.get("chosen_b") or []))
    else:
        raise ValueError(f"unknown job kind {kind!r}")


def run_ingest(env: Env, job_id: str, filename: str) -> None:
    from read import pipeline

    job = env.temp.get_json(job_id, "job.json")

    def log(msg: str) -> None:
        job["log"].append(msg)
        env.temp.put_json(job_id, "job.json", job)

    try:
        data = env.corpus.get(filename)
        meta = env.corpus.get_json(f"{filename}.meta.json") or {}
        if data is None:
            raise pipeline.IngestError(f"{filename} is not in the sources bucket")
        job["result"] = pipeline.ingest_file(data, filename, meta, env.stores(), log)
        job["state"] = "done"
    except Exception as e:  # report every failure to the page
        job["log"].append(f"failed: {e}")
        job["state"] = "failed"
    env.temp.put_json(job_id, "job.json", job)


def review_deps(env: Env) -> dict:
    """Embed/search/Nova for the review threads; each thread gets its own clients."""
    from read import service
    from read.embed import embed
    from read.retrieve import parse_band

    local = threading.local()

    def st():
        if not hasattr(local, "st"):
            local.st = env.stores()
        return local.st

    def search(query: str, goal: dict) -> list[dict]:
        band = parse_band(goal["grade_band"]) or (None, None)
        opts = service.SearchOptions(
            grade_min=band[0], grade_max=band[1], max_results=REVIEW_RESEARCH
        )
        return service.search(query, st(), options=opts)["excerpts"]

    class Client:  # one bedrock-runtime client per thread
        def converse(self, **kw):
            return st().bedrock.converse(**kw)

    return {"embed": lambda t: embed(t, st().bedrock), "search": search, "client": Client()}


def run_review(env: Env, rid: str, chosen_b: set[str]) -> None:
    from read import review_run

    state = {"state": "running", "phase": "Starting", "done": 0, "total": 0}
    last = [0.0]
    lock = threading.Lock()

    def progress(**kw):
        with lock:
            state.update(kw)
            if time.monotonic() - last[0] >= STATE_EVERY:
                last[0] = time.monotonic()
                env.temp.put_json(rid, "state.json", state)

    try:
        material = env.temp.get_json(rid, "material.json")
        goal = env.temp.get_json(rid, "goal.json")
        result = review_run.run(
            material, goal, chosen_b, env.checklist(), review_deps(env), progress
        )
        env.temp.put_json(rid, "review.json", result)
        state = {"state": "done"}
    except Exception as e:  # report the failure to the page
        state = {"state": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
    env.temp.put_json(rid, "state.json", state)
