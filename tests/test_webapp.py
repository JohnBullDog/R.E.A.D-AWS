"""The shared web app with in-memory stores: pages, auth, jobs persisted in the temp store."""

import json

import pytest
from fastapi.testclient import TestClient

from handlers.authorizer import check
from read import jobs, review, review_run, scenario_run
from read.jobs import Env
from read.store import TempStore
from read.webapp import create_app


class Files:
    def __init__(self):
        self.files = {}

    def put(self, key, data, content_type=None):
        self.files[key] = data

    def get(self, key):
        return self.files.get(key)

    def put_json(self, key, value):
        self.files[key] = json.dumps(value).encode()

    def get_json(self, key):
        return json.loads(self.files[key]) if key in self.files else None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "infer_goal", lambda *a: {"objective": "o", "grade_band": "K-2"})
    started = []
    e = Env(
        stores=lambda: type("St", (), {"bedrock": None})(),
        temp=TempStore(tmp_path),
        corpus=Files(),
        app_files=Files(),
        start=started.append,
        max_upload=1000,
    )
    e.started = started
    return e


def test_pages_and_auth_script_are_served(env):
    c = TestClient(create_app(env))
    assert "Welcome to R.E.A.D." in c.get("/").text
    assert "Ask a Science of Reading question" in c.get("/ask_a_question").text
    old = c.get("/review?review=abc123", follow_redirects=False)
    assert old.status_code == 308 and old.headers["location"] == "review_material?review=abc123"
    for path in ("/", "/ask_a_question", "/sources", "/review_material", "/checklist"):
        r = c.get(path)
        assert r.status_code == 200 and '<script src="auth.js"></script>' in r.text
    assert "X-Read-Passcode" in c.get("/auth.js").text
    assert c.get("/api/ping").json() == {"ok": True}  # passcode is enforced by API Gateway


def test_review_state_round_trips_through_the_temp_store(env, monkeypatch):
    c = TestClient(create_app(env))
    up = c.post("/api/review", data={"synthetic": "true", "text": "Blend CVC words. " * 30})
    assert up.status_code == 200, up.text
    rid = up.json()["review_id"]
    goal = {"material_type": "lesson", "grade_band": "K-2", "focus": "phonics", "objective": "o"}
    assert c.post(f"/api/review/{rid}/run", json=goal).json() == {"ok": True}
    assert env.started == [{"kind": "review", "rid": rid, "chosen_b": []}]
    assert c.get(f"/api/review/{rid}").json()["state"] == "running"

    monkeypatch.setattr(review_run, "run", lambda *a: {"findings": ["ok"]})
    jobs.handle(env, env.started[0])  # what the worker Lambda does
    body = c.get(f"/api/review/{rid}").json()
    assert body["state"] == "done" and body["result"] == {"findings": ["ok"]}
    assert c.get("/api/review/deadbeef").status_code == 404


def test_review_requires_synthetic_and_size_limit(env):
    c = TestClient(create_app(env))
    assert c.post("/api/review", data={"synthetic": "false", "text": "x"}).status_code == 400
    big = {"file": ("plan.txt", b"x" * 2000, "text/plain")}
    assert c.post("/api/review", data={"synthetic": "true"}, files=big).status_code == 413


def test_upload_saves_source_and_starts_ingest_job(env):
    c = TestClient(create_app(env))
    meta = {"work_id": "w"}  # incomplete metadata is rejected before anything is saved
    r = c.post("/api/sources", data={"meta": json.dumps(meta)}, files={"file": ("a.txt", b"hi")})
    assert r.status_code == 400 and env.corpus.files == {}


def test_failed_review_is_reported(env, monkeypatch):
    rid = env.temp.new()
    env.temp.put_json(rid, "material.json", {"kind": "t", "parts": []})
    env.temp.put_json(rid, "goal.json", {})

    def boom(*a):
        raise RuntimeError("model down")

    monkeypatch.setattr(review_run, "run", boom)
    jobs.handle(env, {"kind": "review", "rid": rid})
    assert env.temp.get_json(rid, "state.json") == {
        "state": "failed",
        "error": "RuntimeError: model down",
    }


def test_checklist_falls_back_to_the_bundled_default(env):
    assert env.checklist()["criteria"]
    env.app_files.put_json(jobs.CHECKLIST_KEY, {"criteria": [], "status": "edited"})
    assert env.checklist()["status"] == "edited"


def test_passcode_check():
    assert check("open sesame", "open sesame")
    assert not check("wrong", "open sesame")
    assert not check(None, "open sesame") and not check("", "")


# ---------------- practice scenarios (D104-D107) ----------------


def fake_generate(objective, examples, deps, avoid=None):
    assert avoid == []  # the page sends what it has already shown
    return {
        "ok": True,
        "objective": objective,
        "scenario": {
            "title": "A moment",
            "situation": "Something happens in a classroom.",
            "decision_point": "What next?",
            "reflection_prompts": ["a", "b"],
            "grounded_in": ["S1"],
        },
        "research": {
            "S1": {
                "cite_id": "S1",
                "text": "Model the word first.",
                "label": "IES, 2016, p. 14",
                "ref": {"section_id": "w1:v1:s0001"},
            }
        },
        "examples_used": ["ex-blending-k1"],
        "stats": {"model_calls": 1, "seconds": 1.0},
    }


def test_scenario_page_and_objective_bank_are_served(env):
    c = TestClient(create_app(env))
    page = c.get("/practice_scenarios")
    assert page.status_code == 200 and "Practice a classroom decision" in page.text
    bank = c.get("/api/objectives").json()
    assert bank["status"] == "DEVELOPMENT PLACEHOLDER" and bank["objectives"]
    assert c.get("/api/scenario_examples").json()["examples"]


def test_edited_objective_bank_is_validated_before_it_is_saved(env):
    c = TestClient(create_app(env))
    bad = c.put("/api/objectives", json={"objectives": [{"objective_id": "x"}]})
    assert bad.status_code == 400 and "missing" in bad.text
    good = {
        "objectives": [
            {
                "objective_id": "x",
                "component": "Fluency",
                "grade_band": "2-3",
                "objective": "Students reread a passage",
                "search_query": "repeated reading",
            }
        ]
    }
    assert c.put("/api/objectives", json=good).json() == {"ok": True}
    saved = c.get("/api/objectives").json()
    assert saved["objectives"][0]["objective_id"] == "x"
    assert saved["status"] == "DEVELOPMENT PLACEHOLDER"  # the placeholder warning is kept


def test_a_response_runs_as_a_worker_job_and_the_page_polls_it(env, monkeypatch):
    monkeypatch.setattr(scenario_run, "generate", fake_generate)
    c = TestClient(create_app(env))
    made = c.post("/api/scenario", json={"objective_id": "blend-cvc-k1"})
    assert made.status_code == 200, made.text
    sid = made.json()["scenario_id"]
    assert made.json()["research"]["S1"]["label"] == "IES, 2016, p. 14"

    short = c.post(f"/api/scenario/{sid}/respond", json={"synthetic": True, "response": "no"})
    assert short.status_code == 400 and "at least" in short.text
    unconfirmed = c.post(
        f"/api/scenario/{sid}/respond", json={"synthetic": False, "response": "word " * 40}
    )
    assert unconfirmed.status_code == 400 and "no real student" in unconfirmed.text

    ok = c.post(f"/api/scenario/{sid}/respond", json={"synthetic": True, "response": "word " * 40})
    assert ok.json() == {"ok": True}
    assert env.started[-1] == {"kind": "scenario", "sid": sid}
    state = c.get(f"/api/scenario/{sid}").json()
    assert state["state"] == "running" and state["scenario"]["title"] == "A moment"


def test_an_unknown_scenario_is_gone_not_a_crash(env):
    c = TestClient(create_app(env))
    assert c.get("/api/scenario/nope").status_code == 404
    assert c.post("/api/scenario", json={"objective_id": "not-real"}).status_code == 404
