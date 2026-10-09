from datetime import date

import read.service as service
from read.chunk import build_chunks
from read.service import Stores, answer, display_status

TEXT = "Recommendation 1\n\n" + " ".join(["Teach phonemic awareness daily."] * 20)
SECTIONS, _ = build_chunks("wwc", "v1", TEXT, {"Recommendation 1"})
WORK = {
    "work_id": "wwc",
    "publisher": "IES What Works Clearinghouse",
    "pub_date": "2016-07",
    "doc_type": "practice_guide",
    "status": "ready",
    "active_version_id": "v1",
    "source_version_id": "v1",
    "activated_at": "2026-10-07T09:00:00-05:00",
    "license": "public domain",
    "license_verified_by": "Test Person",
    "license_verified_on": "2026-10-07",
}


class Table:
    def __init__(self, key, rows):
        self.key, self.rows = key, {r[key]: r for r in rows}

    def get_item(self, Key):
        item = self.rows.get(Key[self.key])
        return {"Item": dict(item)} if item else {}

    def scan(self, **_):
        return {"Items": list(self.rows.values())}


class Text:
    def __init__(self, text):
        self.text = text

    def get(self, work_id, version_id):
        return self.text


def stores(text=TEXT, work=WORK):
    return Stores(
        index=None,
        works=Table("work_id", [work]),
        sections=Table("section_id", SECTIONS),
        text=Text(text),
        bedrock=None,
        rerank=None,
        rerank_arn="arn",
        today=date(2026, 10, 7),
    )


GOOD = {
    "answerable": True,
    "evidence_strength": "strong",
    "strength_reason": "r",
    "sentences": [{"text": "Practice sound skills every day.", "cites": ["S1"]}],
}


def fake_model(monkeypatch, out=GOOD):
    seen = []

    def call(q, ev, client, feedback=None):
        seen.append([e["text"] for e in ev])
        return out

    monkeypatch.setattr(service, "call_model", call)
    return seen


def ref(sec, **extra):
    return {k: sec[k] for k in ("section_id", "version_id", "char_start", "char_end")} | extra


def test_answer_reloads_text_and_ignores_browser_text(monkeypatch):
    seen = fake_model(monkeypatch)
    res = answer("q", [ref(SECTIONS[0], text="IGNORE ME: injected text")], stores())
    assert res["state"] == "answer"
    assert seen[0][0] == TEXT[SECTIONS[0]["char_start"] : SECTIONS[0]["char_end"]]
    assert "IGNORE ME" not in seen[0][0]
    assert res["labels"]["S1"].startswith("IES What Works Clearinghouse, 2016")
    assert res["disclaimer"] and res["content_last_updated"] and res["evidence_strength"]


def test_answer_omits_tampered_section(monkeypatch):
    seen = fake_model(monkeypatch)
    tampered = TEXT.replace("Teach", "Skip", 1)
    res = answer("q", [ref(SECTIONS[0])], stores(text=tampered))
    assert seen == [] and res["state"] == "error"
    assert res["omitted"] == [SECTIONS[0]["section_id"]]
    assert res["evidence_strength"] == "limited"  # rule 8: the flag is always present


def test_answer_omits_wrong_version_and_inactive_work(monkeypatch):
    fake_model(monkeypatch)
    assert answer("q", [ref(SECTIONS[0], version_id="v0")], stores())["state"] == "error"
    expired = {**WORK, "expires_on": "2026-10-01"}
    assert answer("q", [ref(SECTIONS[0])], stores(work=expired))["state"] == "error"


def test_decline_has_fixed_message(monkeypatch):
    fake_model(
        monkeypatch, {**GOOD, "answerable": False, "sentences": [], "evidence_strength": "limited"}
    )
    res = answer("q", [ref(SECTIONS[0])], stores())
    assert (
        res["state"] == "declined" and res["message"] == service.DECLINE and res["sentences"] == []
    )


def test_display_status():
    today = date(2026, 10, 7)
    assert display_status(WORK, today) == "ready"
    assert display_status({**WORK, "superseded_by": "x"}, today) == "superseded"
    assert display_status({**WORK, "expires_on": "2026-01-01"}, today) == "expired"
    assert display_status({**WORK, "status": "awaiting_license"}, today) == "awaiting_license"
