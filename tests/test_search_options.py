from read.chunk import build_chunks
from read.retrieve import expand, grades_overlap, parse_band
from read.service import SearchOptions
from read.store import pipeline_body


def test_parse_band():
    assert parse_band("K-3") == (0, 3)
    assert parse_band("K-12") == (0, 12)
    assert parse_band("2") == (2, 2)
    assert parse_band("Pre-K") is None or parse_band("PreK-2") is None  # odd forms are ignored
    assert parse_band("high school") is None


def test_grades_overlap():
    k3 = {"grade_bands": ["K-3"]}
    assert grades_overlap(k3, 0, 0) and grades_overlap(k3, 2, 5)
    assert not grades_overlap(k3, 4, 5)
    assert grades_overlap({"grade_bands": ["K-12"]}, 4, 5)
    assert grades_overlap({}, 4, 5)  # untagged sources are kept
    assert grades_overlap(k3, None, None)
    assert grades_overlap(k3, None, 0) and not grades_overlap(k3, 4, None)


def test_options_are_clamped():
    o = SearchOptions(
        grade_min=5, grade_max=1, max_results=99, min_score=-1, candidates=5, keyword_weight=3
    ).clamped()
    assert (o.grade_min, o.grade_max) == (1, 5)
    assert (o.max_results, o.min_score, o.candidates, o.keyword_weight) == (15, 0.0, 10, 1.0)


def test_pipeline_weights_sum_to_one():
    w = pipeline_body(0.75)["phase_results_processors"][0]["normalization-processor"]
    assert w["combination"]["parameters"]["weights"] == [0.75, 0.25]


def test_expand_stops_at_max_sections_but_keeps_highlights():
    body = " ".join(["word"] * 60) + "."
    text = "\n\n".join(f"Chapter {i}\n\n{body}\n\n{body}" for i in range(4))
    sections, passages = build_chunks("w", "v", text, target=30)
    by_id = {s["section_id"]: s for s in sections}
    ranked = [(p, 1.0) for p in passages]
    ev = expand(ranked, by_id.__getitem__, lambda w, v: text, max_sections=2)
    assert len(ev) == 2
    assert all(len(e["hits"]) >= 2 for e in ev)  # later passages of kept sections still highlight
