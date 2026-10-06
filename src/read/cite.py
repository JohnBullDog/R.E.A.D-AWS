"""Citation labels rendered from source metadata only.

The model emits section IDs (S1, S2, ...); nothing it returns reaches a label. See
docs/design.md, "Citation rendering".
"""


def cite_label(section: dict, work: dict) -> str:
    """e.g. "IES What Works Clearinghouse, 2016, p. 12"."""
    if section["work_id"] != work["work_id"]:
        raise ValueError(f"section {section['section_id']} is not from work {work['work_id']}")
    where = f"p. {section['page']}" if section.get("page") else section.get("section_path") or ""
    parts = [work["publisher"], work["pub_date"][:4], where]
    return ", ".join(p for p in parts if p)


def labels(evidence: list[dict], works: dict[str, dict]) -> dict[str, str]:
    """Map each evidence item's cite_id to its label."""
    return {
        e["cite_id"]: cite_label(e["section"], works[e["section"]["work_id"]]) for e in evidence
    }


def last_updated(works: dict[str, dict]) -> str | None:
    """Latest activated_at across ready works: the "Content last updated" date."""
    dates = [
        w["activated_at"]
        for w in works.values()
        if w.get("status") == "ready" and w.get("activated_at")
    ]
    return max(dates) if dates else None
