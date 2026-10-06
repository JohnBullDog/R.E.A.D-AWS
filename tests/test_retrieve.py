import logging

from read.chunk import build_chunks, sha
from read.retrieve import adjust, excerpt, expand, hybrid_query, number_cites, recency, verified

CANON = (
    "Chapter 1\n\n"
    + " ".join(["phonics"] * 300)
    + "\n\nChapter 2\n\n"
    + " ".join(["fluency"] * 300)
)
SECTIONS, PASSAGES = build_chunks("wwc", "v1", CANON)
SEC_BY_ID = {s["section_id"]: s for s in SECTIONS}


def stores(canon=CANON):
    return SEC_BY_ID.__getitem__, lambda work_id, ver: canon


def test_recency_floor_holds():
    assert recency(2000, 2026) == 0.75  # NRP-era work keeps the floor
    assert recency(2026, 2026) == 1.0
    assert 0.75 < recency(2024, 2026) < 1.0
    assert recency(2021, 2026) == 0.75  # floor is reached after ~4.2 years (open-questions Q9)


def test_superseded_and_inactive_works_excluded():
    works = {
        "a": {"doc_type": "practice_guide", "pub_date": "2016-07", "status": "ready"},
        "b": {
            "doc_type": "practice_guide",
            "pub_date": "2008-01",
            "superseded_by": "a",
            "status": "ready",
        },
        "c": {"doc_type": "practice_guide", "pub_date": "2020-01", "status": "ingesting"},
    }
    evidence = [{"section": {"work_id": w}, "score": 1.0} for w in ("b", "a", "c")]
    kept = adjust(evidence, works, 2026)
    assert [e["section"]["work_id"] for e in kept] == ["a"]


def test_authority_reorders():
    works = {
        "p": {"doc_type": "practitioner_resource", "pub_date": "2026-01"},
        "g": {"doc_type": "practice_guide", "pub_date": "2026-01"},
    }
    evidence = [
        {"section": {"work_id": "p"}, "score": 0.9},
        {"section": {"work_id": "g"}, "score": 0.85},
    ]
    assert [e["section"]["work_id"] for e in adjust(evidence, works, 2026)] == ["g", "p"]


def test_expand_merges_hits_and_slices_whole_sections():
    ranked = [(p, 1.0 - i / 10) for i, p in enumerate(PASSAGES)]
    evidence = expand(ranked, *stores())
    assert len(evidence) == 2
    for e in evidence:
        s = e["section"]
        assert e["text"] == CANON[s["char_start"] : s["char_end"]]
    assert sum(len(e["hits"]) for e in evidence) == len(PASSAGES)


def test_budget_drops_whole_sections_never_truncates():
    ranked = [(p, 1.0) for p in PASSAGES]
    evidence = expand(ranked, *stores(), max_words=350)
    assert len(evidence) == 1  # second section (302 words) doesn't fit; dropped whole
    s = evidence[0]["section"]
    assert evidence[0]["text"] == CANON[s["char_start"] : s["char_end"]]


def test_budget_keeps_smaller_lower_ranked_section():
    big, small = SECTIONS[0], dict(SECTIONS[1])
    canon = CANON
    get = {big["section_id"]: big, small["section_id"]: small}.__getitem__
    p_big = next(p for p in PASSAGES if p["section_id"] == big["section_id"])
    p_small = next(p for p in PASSAGES if p["section_id"] == small["section_id"])
    evidence = expand([(p_big, 0.9), (p_small, 0.8)], get, lambda w, v: canon, max_words=305)
    assert [e["section"]["section_id"] for e in evidence] == [big["section_id"]]


def test_tampered_section_is_omitted_and_logged(caplog):
    tampered = CANON.replace("fluency", "fluenci", 1)
    evidence = expand([(p, 1.0) for p in PASSAGES], *stores(tampered))
    with caplog.at_level(logging.ERROR):
        ok = verified(evidence)
    assert len(ok) == 1 and ok[0]["section"]["section_path"] == "Chapter 1"
    assert "integrity_failure" in caplog.text
    assert all(sha(e["text"]) == e["section"]["text_sha256"] for e in ok)


def test_cites_numbered_after_filtering():
    evidence = number_cites([{"x": 1}, {"x": 2}])
    assert [e["cite_id"] for e in evidence] == ["S1", "S2"]


def test_excerpt_shape_and_relative_highlights():
    e = number_cites(expand([(PASSAGES[0], 1.0)], *stores()))[0]
    x = excerpt(e)
    assert x["text"] == e["text"]
    assert x["ref"]["section_id"] == SECTIONS[0]["section_id"]
    s, t = x["highlights"][0]
    assert x["text"][s:t] == PASSAGES[0]["text"]


def test_hybrid_query_filters_both_branches_to_active_versions():
    body = hybrid_query("blending", [0.1] * 4, ["v1"])
    bm25, knn = body["query"]["hybrid"]["queries"]
    assert bm25["bool"]["filter"] == {"terms": {"version_id": ["v1"]}}
    assert knn["knn"]["embedding"]["filter"] == {"terms": {"version_id": ["v1"]}}
    assert body["_source"] == {"excludes": ["embedding"]}
