import json
from pathlib import Path

import pytest

from read.ingest import check_meta, license_ok, local_version_id, work_record
from read.store import LocalTextStore, WriteOnceError

GOOD = {
    "work_id": "wwc-foundational-k3-2016",
    "title": "Foundational Skills",
    "publisher": "IES What Works Clearinghouse",
    "url": "https://ies.ed.gov/x.pdf",
    "pub_date": "2016-07",
    "doc_type": "practice_guide",
}
VERIFIED = {
    **GOOD,
    "license": "US public domain",
    "license_verified_by": "Addison Robertson",
    "license_verified_on": "2026-10-07",
}


def test_check_meta_ok_and_failures():
    assert check_meta(GOOD) == []
    assert "missing publisher" in check_meta({**GOOD, "publisher": ""})
    assert check_meta({**GOOD, "doc_type": "blog"})[0].startswith("doc_type must be one of")
    assert check_meta({**GOOD, "pub_date": "July 2016"}) == ["pub_date must be YYYY or YYYY-MM"]
    assert check_meta({**GOOD, "expires_on": "soon"}) == ["expires_on must be YYYY-MM-DD"]
    assert check_meta({**GOOD, "work_id": "Bad ID"})


def test_license_gate_needs_all_three_fields_and_a_real_date():
    assert license_ok(VERIFIED)
    assert not license_ok(GOOD)
    assert not license_ok({**VERIFIED, "license_verified_by": ""})
    assert not license_ok({**VERIFIED, "license_verified_on": "yesterday"})
    assert not license_ok({**VERIFIED, "license_verified_on": None})


def test_claude_drafts_never_pass_the_gate():
    draft = {
        **GOOD,
        "license": "DRAFT, unverified: public domain",
        "license_verified_by": "",
        "license_verified_on": "",
    }
    assert not license_ok(draft)


def test_corpus_meta_files_are_valid():
    for f in Path("corpus").glob("*.meta.json"):
        assert check_meta(json.loads(f.read_text(encoding="utf-8"))) == [], f


def test_version_id_is_stable_per_content():
    assert local_version_id(b"abc") == local_version_id(b"abc")
    assert local_version_id(b"abc") != local_version_id(b"abd")
    assert local_version_id(b"abc").startswith("sha-")


def test_work_record_keeps_tags_and_state():
    row = work_record(VERIFIED, "sha-1", "f.pdf", "w/sha-1.txt", 10, "ready", "sha-1", "2026-10-07")
    assert row["publisher"] == "IES What Works Clearinghouse"
    assert row["license_verified_by"] == "Addison Robertson"
    assert (row["status"], row["active_version_id"], row["passage_count"]) == ("ready", "sha-1", 10)
    assert "expires_on" not in row  # empty values are left out


def test_text_store_is_write_once(tmp_path):
    store = LocalTextStore(tmp_path)
    key = store.put("w", "v1", "canonical text")
    assert key == "w/v1.txt" and store.get("w", "v1") == "canonical text"
    assert store.put("w", "v1", "canonical text") == key  # same content: fine
    with pytest.raises(WriteOnceError):
        store.put("w", "v1", "different text")
