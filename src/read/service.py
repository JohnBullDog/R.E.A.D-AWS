"""The /search and /answer logic, shared by the local dev server and (later) the Lambdas.

/search: embed the question, hybrid search over active versions, rerank, expand to whole
sections, apply authority/recency, hash-check, and return verbatim excerpts.
/answer: re-load the same sections by ref (never trusting text from the browser), re-check
hashes, ask the answer model, validate, and return cited sentences. See docs/design.md.
"""

import os
import time
from dataclasses import dataclass, field
from datetime import date

from read.answer import call_model, cited_answer
from read.chunk import sha
from read.cite import cite_label, last_updated
from read.embed import embed
from read.retrieve import (
    CANDIDATES,
    QUERY_MAX_CHARS,
    TOP_N,
    active_versions,
    adjust,
    excerpt,
    expand,
    grades_overlap,
    hybrid_query,
    is_active,
    number_cites,
    rerank,
    verified,
)
from read.store import INDEX, KEYWORD_WEIGHT, PIPELINE, pipeline_body

DISCLAIMER = (
    "Advisory only. R.E.A.D. answers are machine-generated from the research excerpts shown and "
    "can contain errors. Read the excerpts and use your professional judgment."
)
DECLINE = (
    "The research in R.E.A.D. doesn't answer this question. The closest excerpts are shown "
    "for reference, but they are not an answer."
)
ANSWER_ERROR = "The answer couldn't be generated. The excerpts above are verified source text."


@dataclass
class SearchOptions:
    """Per-search settings from the Test page; out-of-range values are clamped."""

    grade_min: int | None = None  # -1 = pre-K, 0 = K, 1..12
    grade_max: int | None = None
    max_results: int = TOP_N  # whole sections returned
    min_score: float = 0.0  # drop sections whose best passage scored below this
    candidates: int = CANDIDATES  # passages pulled from hybrid search
    use_reranker: bool = True
    keyword_weight: float = KEYWORD_WEIGHT  # 0 = meaning only, 1 = keywords only

    def clamped(self) -> "SearchOptions":
        def grade(g):
            return None if g is None else min(max(int(g), -1), 12)

        lo, hi = grade(self.grade_min), grade(self.grade_max)
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        return SearchOptions(
            grade_min=lo,
            grade_max=hi,
            max_results=min(max(int(self.max_results), 1), 15),
            min_score=min(max(float(self.min_score), 0.0), 1.0),
            candidates=min(max(int(self.candidates), 10), 100),
            use_reranker=bool(self.use_reranker),
            keyword_weight=min(max(float(self.keyword_weight), 0.0), 1.0),
        )


@dataclass
class Stores:
    """Clients for one environment (local Docker or AWS)."""

    os: object  # OpenSearch client
    works: object  # DynamoDB works table
    sections: object  # DynamoDB sections table
    text: object  # canonical text store with get(work_id, version_id)
    bedrock: object  # bedrock-runtime client (embeddings + answer model)
    rerank: object  # bedrock-agent-runtime client in RERANK_REGION
    rerank_arn: str
    today: date = field(default_factory=date.today)

    @classmethod
    def local(cls, root, profile: str = "read-poc") -> "Stores":
        import boto3
        from botocore.config import Config

        from read.store import (
            SECTIONS_TABLE,
            WORKS_TABLE,
            LocalTextStore,
            dynamodb_resource,
            ensure_tables,
            opensearch_client,
        )

        ddb = dynamodb_resource("http://localhost:8000")
        ensure_tables(ddb)
        session = boto3.Session(profile_name=profile, region_name="us-east-1")
        retry = Config(retries={"max_attempts": 8, "mode": "adaptive"})
        region = os.environ.get("RERANK_REGION", "us-west-2")
        return cls(
            os=opensearch_client("http://localhost:9200"),
            works=ddb.Table(WORKS_TABLE),
            sections=ddb.Table(SECTIONS_TABLE),
            text=LocalTextStore(root / "data" / "works-text"),
            bedrock=session.client("bedrock-runtime", config=retry),
            rerank=session.client("bedrock-agent-runtime", region_name=region, config=retry),
            rerank_arn=os.environ.get(
                "RERANK_MODEL_ARN",
                f"arn:aws:bedrock:{region}::foundation-model/amazon.rerank-v1:0",
            ),
        )


def load_works(st: Stores) -> dict[str, dict]:
    items, kwargs = [], {}
    while True:
        page = st.works.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return {w["work_id"]: w for w in items}
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def display_status(work: dict, today: date) -> str:
    """One word for the Sources page: ready, superseded, expired, awaiting_license, ..."""
    status = work.get("status", "unknown")
    if status != "ready":
        return status
    if work.get("superseded_by"):
        return "superseded"
    if not is_active(work, today):
        return "expired"
    return "ready"


def _int(x) -> int:
    return int(x)  # DynamoDB returns Decimal


def _section_text(st: Stores, sec: dict) -> str:
    whole = st.text.get(sec["work_id"], sec["version_id"])
    return whole[_int(sec["char_start"]) : _int(sec["char_end"])]


def _envelope(works: dict[str, dict], st: Stores) -> dict:
    return {"disclaimer": DISCLAIMER, "content_last_updated": last_updated(works)}


def search(q: str, st: Stores, debug: bool = False, options: SearchOptions | None = None) -> dict:
    t0 = time.perf_counter()
    opt = (options or SearchOptions()).clamped()
    q = (q or "").strip()[:QUERY_MAX_CHARS]
    works = load_works(st)
    out = {"query": q, "excerpts": [], **_envelope(works, st)}
    if not q:
        return {**out, "message": "Type a question."}
    in_grades = {
        w["work_id"] for w in works.values() if grades_overlap(w, opt.grade_min, opt.grade_max)
    }
    active = active_versions({k: w for k, w in works.items() if k in in_grades}, st.today)
    if not active:
        msg = "No active sources for that grade range." if len(in_grades) < len(works) else None
        return {**out, "message": msg or "No sources are active yet."}
    vec = embed(q, st.bedrock)
    t1 = time.perf_counter()
    body = hybrid_query(q, vec, active, size=opt.candidates)
    params = {"search_pipeline": PIPELINE}
    if abs(opt.keyword_weight - KEYWORD_WEIGHT) > 1e-9:  # one-off weights: inline pipeline
        body["search_pipeline"] = pipeline_body(opt.keyword_weight)
        params = {}
    hits = st.os.search(index=INDEX, body=body, params=params)["hits"]["hits"]
    t2 = time.perf_counter()
    candidates = [h["_source"] for h in hits]
    if opt.use_reranker:
        ranked = rerank(q, candidates, st.rerank, st.rerank_arn, n=len(candidates))
    else:
        ranked = [(h["_source"], h["_score"]) for h in hits]
    below = [(p, sc) for p, sc in ranked if sc < opt.min_score]
    ranked = [(p, sc) for p, sc in ranked if sc >= opt.min_score]
    t3 = time.perf_counter()
    sections_cache: dict[str, dict] = {}

    def get_section(sid: str) -> dict:
        if sid not in sections_cache:
            sections_cache[sid] = st.sections.get_item(Key={"section_id": sid})["Item"]
        return sections_cache[sid]

    evidence = expand(ranked, get_section, st.text.get, max_sections=opt.max_results)
    evidence = number_cites(verified(adjust(evidence, works, st.today)))
    for e in evidence:
        x = excerpt(e)
        work = works[e["section"]["work_id"]]
        x["ref"] = {k: (_int(v) if k.startswith("char_") else v) for k, v in x["ref"].items()}
        x.update(
            label=cite_label(e["section"], work),
            title=work.get("title"),
            url=work.get("url"),
            page=_int(e["section"]["page"]) if e["section"].get("page") else None,
            grade_bands=work.get("grade_bands"),
        )
        out["excerpts"].append(x)
    if not out["excerpts"]:
        out["message"] = (
            "No sections met the minimum relevance score."
            if below and opt.min_score > 0
            else "No relevant sections found."
        )
    if debug:
        out["debug"] = {
            "options": vars(opt),
            "timing_s": {
                "embed": round(t1 - t0, 2),
                "search": round(t2 - t1, 2),
                "rerank": round(t3 - t2, 2) if opt.use_reranker else 0,
                "total": round(time.perf_counter() - t0, 2),
            },
            "sources_in_grade_range": sorted(in_grades),
            "candidates": len(candidates),
            "below_min_score": len(below),
            "hybrid_top": [
                {"score": round(h["_score"], 4), "chunk_id": h["_source"]["chunk_id"]}
                for h in hits[:10]
            ],
            "ranked_top": [
                {"score": round(sc, 5), "chunk_id": p["chunk_id"], "section": p.get("section_path")}
                for p, sc in ranked[:15]
            ],
            "final_scores": {e["cite_id"]: round(e["score"], 5) for e in evidence},
        }
    return out


def answer(q: str, refs: list[dict], st: Stores, debug: bool = False) -> dict:
    """Cited answer for the excerpts /search returned. Only refs come from the browser."""
    t0 = time.perf_counter()
    q = (q or "").strip()[:QUERY_MAX_CHARS]
    works = load_works(st)
    evidence, omitted = [], []
    for ref in refs[:16]:
        item = st.sections.get_item(Key={"section_id": str(ref.get("section_id", ""))}).get("Item")
        if not item or item["version_id"] != ref.get("version_id"):
            omitted.append(ref.get("section_id"))
            continue
        work = works.get(item["work_id"])
        if (
            not work
            or not is_active(work, st.today)
            or work.get("active_version_id") != item["version_id"]
        ):
            omitted.append(item["section_id"])
            continue
        text = _section_text(st, item)
        if sha(text) != item["text_sha256"]:  # verified() logs the integrity failure
            verified([{"section": item, "text": text}])
            omitted.append(item["section_id"])
            continue
        evidence.append({"section": item, "text": text, "score": 0.0, "hits": []})
    evidence = number_cites(evidence)
    out = {"query": q, **_envelope(works, st)}
    no_answer = {  # rule 8: every response carries an evidence-strength flag
        "state": "error",
        "message": ANSWER_ERROR,
        "evidence_strength": "limited",
        "strength_reason": "No validated answer; read the excerpts directly.",
    }
    if not evidence:
        return {**out, **no_answer, "omitted": omitted}
    res = cited_answer(q, evidence, works, lambda qq, ev, fb: call_model(qq, ev, st.bedrock, fb))
    cite_map = {e["cite_id"]: e["section"]["section_id"] for e in evidence}
    labels = {
        e["cite_id"]: cite_label(e["section"], works[e["section"]["work_id"]]) for e in evidence
    }
    if not res["ok"]:
        out.update(no_answer)
    else:
        a = res["answer"]
        out.update(
            state="answer" if a["answerable"] else "declined",
            message=None if a["answerable"] else DECLINE,
            sentences=a["sentences"] if a["answerable"] else [],
            evidence_strength=res["evidence_strength"],
            strength_reason=a["strength_reason"],
            strength_capped_by=res["strength_capped_by"],
        )
    out.update(cite_map=cite_map, labels=labels, omitted=omitted)
    if debug:
        out["debug"] = {
            "model": os.environ.get("ANSWER_MODEL_ID"),
            "timing_s": round(time.perf_counter() - t0, 2),
            "attempts": res.get("attempts", []),
        }
    return out
