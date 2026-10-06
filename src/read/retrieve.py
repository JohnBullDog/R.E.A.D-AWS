"""Hybrid search query, authority/recency ranking, section expansion, hash checks.

See docs/design.md, "Retrieval" and "Ranking by authority and recency". Store access is
passed in as functions so everything here is testable without AWS.
"""

import logging
from collections.abc import Callable
from datetime import date

from read.chunk import sha

log = logging.getLogger(__name__)

CANDIDATES = 40
TOP_N = 8  # sections kept after reranking
MIN_TOP_SCORE = 0.01  # below this best rerank score: "no relevant sections" (tune on golden set)
MAX_WORDS = 9000  # evidence budget; whole sections are dropped, never truncated
QUERY_MAX_CHARS = 500

AUTHORITY = {  # starting weights; tune with the SME
    "practice_guide": 1.00,  # IES/WWC practice guides
    "systematic_review": 1.00,  # meta-analyses, NRP report
    "state_standard": 1.00,  # MS CCRS for ELA, LBPA materials
    "peer_reviewed_study": 0.95,
    "federal_report": 0.90,  # NICHD, NAEP aggregates
    "practitioner_resource": 0.80,  # NCIL, FCRR
}


def hybrid_query(
    q: str, vector: list[float], active_versions: list[str], size: int = CANDIDATES
) -> dict:
    """OpenSearch body for BM25 + k-NN, both filtered to active versions.

    Run with params={"search_pipeline": "hybrid-norm"} so scores are normalized and combined.
    """
    versions = {"terms": {"version_id": active_versions}}
    return {
        "size": size,
        "_source": {"excludes": ["embedding"]},
        "query": {
            "hybrid": {
                "queries": [
                    {"bool": {"must": {"match": {"text": q}}, "filter": versions}},
                    {"knn": {"embedding": {"vector": vector, "k": size, "filter": versions}}},
                ]
            }
        },
    }


def rerank(
    q: str,
    passages: list[dict],
    client,
    model_arn: str,
    n: int = TOP_N,
    min_top_score: float = MIN_TOP_SCORE,
) -> list[tuple[dict, float]]:
    """Re-order candidates with the Bedrock Rerank API and keep the top n.

    Amazon Rerank scores are nearly all-or-nothing, so they decide order only; the single
    best score decides whether anything is relevant (Q13). An empty result means "no
    relevant sections found". client is a boto3 bedrock-agent-runtime client in
    RERANK_REGION; model_arn comes from RERANK_MODEL_ARN.
    """
    if not passages:
        return []
    res = client.rerank(
        queries=[{"type": "TEXT", "textQuery": {"text": q}}],
        sources=[
            {
                "type": "INLINE",
                "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": p["text"]}},
            }
            for p in passages
        ],
        rerankingConfiguration={
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "numberOfResults": min(n, len(passages)),
                "modelConfiguration": {"modelArn": model_arn},
            },
        },
    )
    ranked = [(passages[r["index"]], r["relevanceScore"]) for r in res["results"]]
    ranked.sort(key=lambda pr: pr[1], reverse=True)
    if not ranked or ranked[0][1] < min_top_score:
        return []
    return ranked


def recency(pub_year: int, now_year: int, half_life: float = 10, floor: float = 0.75) -> float:
    """Gentle decay; the floor keeps foundational work like the NRP report competitive."""
    return max(floor, 0.5 ** ((now_year - pub_year) / half_life))


def is_active(work: dict, today: date) -> bool:
    """A work is searchable only if ready, not superseded, and not past its expiration date.

    expires_on is optional (YYYY-MM-DD, from meta.json); on that date the work stops being
    used. Updating a document is a re-upload: the new version ingests alongside the old one
    and replaces it only after verification (see docs/design.md, "Failure cases").
    """
    if work.get("status") != "ready" or work.get("superseded_by"):
        return False
    expires = work.get("expires_on")
    return not expires or date.fromisoformat(expires) > today


def active_versions(works: dict[str, dict], today: date) -> list[str]:
    """version_ids the search filter allows; inactive works never reach the index query."""
    return sorted(
        w["active_version_id"]
        for w in works.values()
        if is_active(w, today) and w.get("active_version_id")
    )


def adjust(evidence: list[dict], works: dict[str, dict], today: date) -> list[dict]:
    """Drop inactive works and scale each score by authority and recency."""
    kept = []
    for e in evidence:
        w = works.get(e["section"]["work_id"])
        if w is None or not is_active(w, today):
            continue
        weight = AUTHORITY[w["doc_type"]] * recency(int(w["pub_date"][:4]), today.year)
        kept.append({**e, "score": e["score"] * weight})
    return sorted(kept, key=lambda e: e["score"], reverse=True)


def expand(
    ranked: list[tuple[dict, float]],
    get_section: Callable[[str], dict],
    get_canonical: Callable[[str, str], str],
    max_words: int = MAX_WORDS,
) -> list[dict]:
    """Group ranked passages by parent section; return whole sections in rank order.

    A section that doesn't fit the word budget is dropped whole, never truncated.
    """
    evidence: list[dict] = []
    by_section: dict[str, dict] = {}
    dropped: set[str] = set()
    words = 0
    for p, score in ranked:
        sid = p["section_id"]
        if sid in by_section:
            by_section[sid]["hits"].append((p["char_start"], p["char_end"]))
            continue
        if sid in dropped:
            continue
        sec = get_section(sid)
        text = get_canonical(sec["work_id"], sec["version_id"])[
            int(sec["char_start"]) : int(sec["char_end"])
        ]
        n = len(text.split())
        if words + n > max_words:
            dropped.add(sid)
            continue
        words += n
        item = {
            "section": sec,
            "text": text,
            "score": score,
            "hits": [(p["char_start"], p["char_end"])],
        }
        by_section[sid] = item
        evidence.append(item)
    return evidence


def verified(evidence: list[dict]) -> list[dict]:
    """Keep only items whose text matches the stored hash; log every mismatch.

    Run after all reordering, then number_cites(). An omitted item never reaches the
    excerpts or the model.
    """
    ok = []
    for e in evidence:
        sec = e["section"]
        if sha(e["text"]) != sec["text_sha256"]:
            log.error(
                "integrity_failure section_id=%s version_id=%s char_start=%s char_end=%s",
                sec["section_id"],
                sec["version_id"],
                sec["char_start"],
                sec["char_end"],
            )
            continue
        ok.append(e)
    return ok


def number_cites(evidence: list[dict]) -> list[dict]:
    """Assign S1, S2, ... in final display order."""
    return [{**e, "cite_id": f"S{i}"} for i, e in enumerate(evidence, 1)]


def excerpt(e: dict) -> dict:
    """The response shape for one excerpt. Text comes only from the canonical file."""
    sec = e["section"]
    base = int(sec["char_start"])
    return {
        "cite_id": e["cite_id"],
        "work_id": sec["work_id"],
        "section_path": sec.get("section_path"),
        "page": sec.get("page"),
        "text": e["text"],
        "highlights": sorted([int(s) - base, int(t) - base] for s, t in e["hits"]),
        "ref": {k: sec[k] for k in ("section_id", "version_id", "char_start", "char_end")},
    }
