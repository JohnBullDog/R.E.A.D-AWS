import pytest

from read.cite import cite_label, labels, last_updated

WORK = {"work_id": "wwc", "publisher": "IES What Works Clearinghouse", "pub_date": "2016-07"}


def test_label_with_page():
    sec = {"section_id": "wwc:v:s0", "work_id": "wwc", "page": 12, "section_path": "Rec 1"}
    assert cite_label(sec, WORK) == "IES What Works Clearinghouse, 2016, p. 12"


def test_label_falls_back_to_section_path():
    sec = {"section_id": "wwc:v:s0", "work_id": "wwc", "section_path": "Recommendation 2"}
    assert cite_label(sec, WORK) == "IES What Works Clearinghouse, 2016, Recommendation 2"


def test_label_ignores_everything_but_metadata():
    # Model-shaped fields riding along on the evidence item must never reach the label.
    e = {
        "cite_id": "S1",
        "text": "Fake Publisher 1999 p. 4",
        "label": "Fake, 1999",
        "section": {"section_id": "wwc:v:s0", "work_id": "wwc", "page": 3},
    }
    assert labels([e], {"wwc": WORK}) == {"S1": "IES What Works Clearinghouse, 2016, p. 3"}


def test_label_rejects_mismatched_work():
    with pytest.raises(ValueError):
        cite_label({"section_id": "x:v:s0", "work_id": "x"}, WORK)


def test_last_updated_uses_ready_works_only():
    works = {
        "a": {"status": "ready", "activated_at": "2026-10-01T00:00:00Z"},
        "b": {"status": "ready", "activated_at": "2026-10-05T00:00:00Z"},
        "c": {"status": "ingesting", "activated_at": "2026-10-06T00:00:00Z"},
    }
    assert last_updated(works) == "2026-10-05T00:00:00Z"
    assert last_updated({}) is None
