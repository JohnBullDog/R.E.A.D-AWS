# Design Doc: R.E.A.D. Research-Grounded Q&A on AWS

Oct 6, 2026 · @John Patton

> **PoC revisions (2026-10-06).** This design is the working plan for the proof of concept.
> The Hub/MDE proposal remains the ground truth for the final product. Changes made during
> the PoC are listed here and logged with reasons in `docs/decisions.md`.
>
> - **R1 Answer model:** must be an Amazon model; Amazon Nova Pro (`amazon.nova-pro-v1:0`) via
>   `ANSWER_MODEL_ID`, `temperature=0`, forced `record_answer` tool. Names use "answer", not
>   "summary" (`cited_answer`, `ANSWER_MODEL_ID`).
> - **R2 PDF extraction:** paragraphs and headings are rebuilt from page layout (line spacing,
>   font size, bold); repeated running headers/footers and page numbers are dropped. Headings
>   come from formatting, not fixed words, because source formatting varies.
> - **R3 Sections:** text under a heading forms a section (split after 1,500 words; the
>   continuation keeps the heading); text with no heading forms sections of at most
>   5 paragraphs or 500 words.
> - **R4 Citations:** a section spanning pages is cited as a range ("pp. 12–13").
> - **R5 Expiration and versions:** each work may set an optional `expires_on` date; from that
>   date it is excluded from search. Updates are re-uploads that become a new version, as in
>   "Failure cases". Inactive works (not ready, superseded, expired) are filtered out before
>   search, so they never use the evidence budget.
> - **R6 Development search:** local OpenSearch in Docker for development (free). The AWS
>   OpenSearch Serverless collection is created only for short, approved tests and demos, and
>   deleted afterward. Spend cap: $10/month without explicit approval.
> - **R8 Reranker:** Amazon Rerank 1.0 in us-west-2 (it isn't offered in us-east-1), per the
>   AWS-model requirement. Cohere Rerank is not used.
> - **R9 Retry with feedback:** the single retry after a failed validation includes the rejected
>   sentences and the problems (e.g. the copied phrase), since a temperature-0 retry with the same
>   prompt repeats the same answer.
> - **R10 Page layout:** NotebookLM-style. The answer is shown first with numbered citation chips
>   that expand to the verbatim excerpts; a Sources list follows (cited first, uncited collapsed).
>   Sources still appear as soon as /search returns. Replaces "excerpts first, then the answer".
> - **R11 Verified quotes:** wording an answer reuses from a cited section (8+ words) is shown as
>   a quotation sliced from the canonical text, max 40 words; reuse from an uncited section fails
>   validation. Replaces "no copied 8-word phrase".
> - **R7 Declines:** an unanswerable result may have no sentences; the page shows a fixed
>   decline message written in code.

## Overview

Decision: build R.E.A.D.'s Research-Grounded Q&A as a custom pipeline on Lambda, Bedrock, and OpenSearch. Teachers must be able to trust every citation, and only a pipeline we control end to end can guarantee that each supporting excerpt is the exact source text, never model output.

**Problem.** Mississippi K-5 teachers complete MDE's Science of Reading training (LBPA, Strong Readers = Strong Leaders) but have no on-demand help applying it between coaching sessions. R.E.A.D. (Reading Educator Assistance Desk) answers their instructional questions in plain language from a corpus of public Science of Reading research, with inline citations and the supporting excerpts. This design covers the Phase 1 core deliverable, Q&A, and the shared pipeline that Phase 2 (Lesson Plan Alignment Review, Coaching Scenario Simulation) builds on.

**Goals**

- Answer instructional questions with responses grounded only in retrieved sources.
- Cite inline with source, publication date, and page or section for every claim.
- Show the supporting excerpts **verbatim from the source**: their text never passes through an LLM, and the teacher can check it.
- Never quote out of context: every excerpt is a whole paragraph or section with its location.
- Flag when evidence is limited, mixed, or contested, and decline to answer when retrieved evidence doesn't support one.
- Favor current, peer-reviewed, authoritative sources over superseded ones, using source tagging by date, publisher, and version.
- Show an advisory-only disclaimer and a content-base "last updated" date on every output.
- Respond in under 3 seconds for excerpts; the answer may stream after.

**Constraints**

- **Formats in v1:** plain text, PDF (text-layer and scanned), and Word (DOC and DOCX). This covers most of the corpus (practice guides, the NRP report, MS CCRS) and Phase 2 lesson-plan uploads. EPUB, HTML, Markdown, and RTF are stretch goals; HTML is likely the first needed, since some NCIL and FCRR resources are web pages.
- **Corpus grows and changes often:** ingestion must keep up with new and revised sources without re-processing unchanged text.
- **Public-facing PoC:** anyone with the link can use it, so abuse, scraping, and spend must be controlled.
- **Every question gets an answer or an explicit decline:** there is no excerpts-only mode. Excerpts without an answer appear only as an error state.

**Sources and rights.** The corpus is public and openly licensed material (IES/WWC practice guides, ERIC, PubMed Central Open Access, National Reading Panel and NICHD, NCIL, FCRR, MS CCRS for ELA, MS LBPA materials, NAEP aggregates) plus synthetic lesson plans and scenarios. The proposal requires each source's license and terms of use to be verified and documented before ingestion, so ingestion enforces that as a gate. No district, teacher, or student data is used. Users are K-5 teachers; the PoC runs through the AI Innovation Hub.

**Scope: proof of concept.** Phase 1 (weeks 1–6) delivers a validated Q\&A; Phase 2 (weeks 6–7) adds Lesson Plan Alignment Review, then Coaching Scenario Simulation, on the same pipeline. No user authentication in this phase: use is anonymous, controlled at the edge with WAF rate limits, API Gateway throttling, and the spend caps under Security. A WAF IP allowlist can restrict the PoC to known networks without any sign-in.

**Non-goals (for v1)**

- Production deployment, PD credit or licensure tracking, and any use in teacher evaluation.
- Any student data, real teacher artifacts, or district data.
- Content outside K-5 Science of Reading.
- Open-ended chat beyond what retrieved sources support.
- User accounts and per-user history.

## Architecture overview

Ingestion writes verified, versioned evidence; the query side reads it by ID and offset. Excerpts and the answer travel on separate paths, so source text never passes through the LLM.

&#91;embedded content: architecture · ingestion and query paths\]

The page calls `/search` first and shows hash-checked excerpts immediately, then calls `/answer`, which re-reads the same sections, asks the LLM for an answer that cites section IDs, and validates every citation before returning it.

## Data model

Every excerpt is addressed by `(work, version, character range)` into an immutable canonical text file, so any citation can be re-fetched and verified byte for byte.

**Canonical text.** At ingest, each work's extracted text is written once to `s3://works-text/<work_id>/<version_id>.txt` (UTF-8, normalized line endings, never edited). All offsets point into this file, not the original PDF or EPUB, because extraction can differ between runs. S3 versioning stays on for both buckets.

**Two chunk levels.** Small *passages* (about 200–300 words) are what we search, because small units embed precisely. Each passage belongs to a parent *section* (a heading-bounded section or a run of 3–5 paragraphs) that is what we show, so excerpts always carry their surrounding context.

### Passage record (OpenSearch index `passages`)

| Field | Type | Purpose |
| --- | --- | --- |
| `chunk_id` | keyword | Stable ID, e.g. `moby-dick:v3:p00412`; cited by the answer |
| `work_id` | keyword | Which work |
| `version_id` | keyword | S3 version ID of the source upload; names the canonical text file and pins the citation |
| `section_id` | keyword | Parent section to display |
| `char_start`, `char_end` | integer | Exact range in the canonical text |
| `text` | text (BM25) | Passage text, for keyword search |
| `embedding` | knn\_vector (1024) | For semantic search |
| `text_sha256` | keyword | Hash of `text`, checked before display |
| `title`, `section_path`, `page` | text / keyword / integer | Display: "Moby-Dick › Ch. 42 › p. 188" |
| `embed_model` | keyword | Model that made the vector, for re-index planning |

### Section record (DynamoDB table `sections`)

`section_id` (key), `work_id`, `version_id`, `char_start`, `char_end`, `section_path`, `text_sha256`. Section text is not duplicated here; it is sliced from the canonical file on demand.

### Work registry (DynamoDB table `works`)

One row per source, holding processing state and the source tagging that drives ranking and citations. Queries filter to each source's `active_version_id`, so a re-upload never mixes old and new passages.

| Field | Example | Purpose |
| --- | --- | --- |
| `work_id` (key) | `wwc-foundational-k3-2016` | Stable source ID |
| `title`, `publisher`, `url` | IES What Works Clearinghouse | Rendered in citations |
| `pub_date`, `version` | 2016-07, v2 | Recency ranking and "which edition" |
| `doc_type` | `practice_guide` | Authority ranking (see Source tagging) |
| `peer_reviewed` | true | Authority ranking |
| `grade_bands`, `components` | K-3; phonics, fluency | Filtering by grade and reading component |
| `superseded_by` | ID of the newer edition | Superseded sources drop out of answers |
| `expires_on` (optional) | 2028-06-30 | From this date the source drops out of answers (PoC revision R5) |
| `license`, `license_verified_by`, `license_verified_on` | US public domain; reviewer; review date | Ingestion gate: no verified license, no activation |
| `source_key`, `source_version_id`, `canonical_key`, `active_version_id` |  | Processing and version pinning |
| `status`, `passage_count`, `activated_at` | `ready` | State; latest `activated_at` is the "last updated" date |

Tagging comes from a `<file>.meta.json` sidecar uploaded with each source and reviewed by the SME; a source without one fails ingestion with `failed: missing metadata`.

## Ingestion

A Step Functions workflow turns each upload into canonical text, sections, and embedded passages, and only makes the new version searchable once every offset has been verified.

### Steps

1. **Upload** the work to `works-raw` (versioning on). EventBridge sends `Object Created` to an SQS queue, which absorbs bursts; a starter Lambda launches `ingest-work` executions up to a concurrency cap.
2. **Supersede stale runs.** The starter records the new `source_version_id` in the `works` table with a conditional write. If a newer version of the same work arrives mid-ingest, the older run sees it at its next step and stops, so frequent updates never activate out of order.
3. **Detect the format by content** (file signature), not extension, and reject anything unsupported with a `failed: unsupported format` status.
4. **Extract text** (Lambda, by format):
   - **Plain text:** detect the encoding with `charset-normalizer`, decode, normalize line endings.
   - **PDF:** `pdfplumber` page by page, recording each page's start offset. If pages come back with little or no text (a scan), send the file to Textract's async API and use its lines instead.
   - **DOCX:** `python-docx`, paragraphs in order; heading styles mark section boundaries, and tables are flattened row by row.
   - **DOC (legacy binary):** convert to DOCX with headless LibreOffice in a container-image Lambda, then treat as DOCX.
   - **Stretch goals:** EPUB (`ebooklib`), HTML (`BeautifulSoup`), Markdown, RTF, each as one more extractor behind the same interface.
5. **Write canonical text** to `works-text/<work_id>/<version_id>.txt` and mark the work `ingesting`.
6. **Chunk** into sections and passages with exact offsets (code below).
7. **Embed with a cache.** Passages whose `text_sha256` already has a vector reuse it; only new or changed text calls Bedrock. On a re-upload with small edits, almost everything is a cache hit.
8. **Index** passages into OpenSearch with the bulk API and write sections to DynamoDB.
9. **Verify and activate**: check counts and offsets and that license\_verified\_on is set, then set `active_version_id` and status `ready`, then delete the previous version's passages.

### Chunker

```python
import hashlib, re

HEADING = re.compile(r"(chapter|part|book|section)\s+\S+", re.I)

def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def paragraphs(text):
    """Yield exact (start, end) offsets of each paragraph in text."""
    for m in re.finditer(r"[^\n](?:.|\n(?!\s*\n))*", text):
        s, e = m.start(), m.end()
        while s < e and text[s].isspace(): s += 1
        while e > s and text[e - 1].isspace(): e -= 1
        if e > s:
            yield s, e

def build_chunks(work_id, ver, text, headings=frozenset(), target=250, max_sec=1500):
    sections, passages = [], []
    sec = psg = None   # [start, end, words, heading] / [start, end, words]

    def close_passage():
        nonlocal psg
        if psg:
            s, e = psg[0], psg[1]
            passages.append({
                "chunk_id": f"{work_id}:{ver}:p{len(passages):05d}",
                "section_id": f"{work_id}:{ver}:s{len(sections):04d}",
                "work_id": work_id, "version_id": ver,
                "char_start": s, "char_end": e,
                "text": text[s:e], "text_sha256": sha(text[s:e])})
            psg = None

    def close_section():
        nonlocal sec
        close_passage()
        if sec:
            s, e = sec[0], sec[1]
            sections.append({
                "section_id": f"{work_id}:{ver}:s{len(sections):04d}",
                "work_id": work_id, "version_id": ver,
                "char_start": s, "char_end": e, "heading": sec[3],
                "text_sha256": sha(text[s:e])})
            sec = None

    for s, e in paragraphs(text):
        para = text[s:e]
        words = len(para.split())
        is_heading = para in headings or (len(para) < 100 and HEADING.match(para))
        if is_heading or (sec and sec[2] + words > max_sec):
            close_section()
        if not sec:
            sec = [s, e, 0, para if is_heading else None]
        sec[1], sec[2] = e, sec[2] + words
        if psg and psg[2] + words > target:
            close_passage()
        if not psg:
            psg = [s, e, 0]
        psg[1], psg[2] = e, psg[2] + words
    close_section()

    for p in passages:   # offsets must reproduce the stored text exactly
        assert text[p["char_start"]:p["char_end"]] == p["text"]
    return sections, passages
```

Passages never cross a section boundary, so a cited passage always maps to one parent. Paragraphs longer than about 400 words should be split further at sentence boundaries; that step is left out above for length. Page numbers come from `bisect` over the page-start offsets recorded at extraction.

### Embed and index (Map state Lambda)

```python
import json, boto3
from opensearchpy import helpers

bedrock = boto3.client("bedrock-runtime")
EMBED_MODEL = "amazon.titan-embed-text-v2:0"

def embed(text):
    r = bedrock.invoke_model(modelId=EMBED_MODEL, body=json.dumps(
        {"inputText": text, "dimensions": 1024, "normalize": True}))
    return json.loads(r["body"].read())["embedding"]

def handler(event, _ctx):
    actions = [{
        "_index": "passages", "_id": p["chunk_id"],
        "_source": {**p, "embedding": embed(p["text"]), "embed_model": EMBED_MODEL}}
        for p in event["passages"]]
    ok, errors = helpers.bulk(os_client, actions, raise_on_error=False)
    if errors:
        raise RuntimeError(f"{len(errors)} passages failed to index")  # Step Functions retries
    return {"indexed": ok}
```

Using `chunk_id` as the document `_id` makes retries idempotent: a re-run overwrites rather than duplicates. Add `version_id`, `section_id`, `char_start`, `char_end`, and `text_sha256` to the index mapping as keyword and integer fields.

### Format detection and extraction

```python
import io, subprocess, tempfile
import pdfplumber, docx
from charset_normalizer import from_bytes

class Unsupported(Exception):
    pass

def sniff(data):
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":                      # zip container
        return "docx"                                   # confirm word/document.xml exists
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # OLE2, legacy .doc
        return "doc"
    return "txt"

def doc_to_docx(data):
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/in.doc", "wb") as f:
            f.write(data)
        subprocess.run(["soffice", "--headless", "--convert-to", "docx",
                        "--outdir", d, f"{d}/in.doc"], check=True, timeout=120)
        with open(f"{d}/in.docx", "rb") as f:
            return f.read()

def extract(data):
    """Return (text, page_start_offsets, heading_paragraph_texts)."""
    kind = sniff(data)
    if kind == "txt":
        best = from_bytes(data).best()
        if best is None:
            raise Unsupported("not decodable as text")
        return str(best).replace("\r\n", "\n"), [], set()
    if kind == "pdf":
        parts, pages = [], []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                pages.append(sum(len(p) for p in parts))
                parts.append((page.extract_text() or "") + "\n\n")
        return "".join(parts), pages, set()   # sparse text -> Textract path
    if kind == "doc":
        data = doc_to_docx(data)
    d = docx.Document(io.BytesIO(data))
    paras = [p for p in d.paragraphs if p.text.strip()]
    headings = {p.text for p in paras if p.style.name.startswith("Heading")}
    return "\n\n".join(p.text for p in paras), [], headings
```

The chunker treats a paragraph as a heading if it matches `HEADING` or appears in the returned `headings` set. LibreOffice in Lambda needs a container image and `HOME=/tmp`.

### Embedding cache

```python
cache = ddb.Table("embedding_cache")   # partition key text_sha256, sort key embed_model

def embed_cached(p):
    key = {"text_sha256": p["text_sha256"], "embed_model": EMBED_MODEL}
    hit = cache.get_item(Key=key).get("Item")
    if hit:
        return json.loads(hit["vector"])
    vec = embed(p["text"])
    cache.put_item(Item={**key, "vector": json.dumps(vec)})
    return vec
```

Swap `embed` for `embed_cached` in the Map state Lambda; in production, use `batch_get_item` for 100 keys at a time.

### Keeping up with frequent updates

- SQS plus the starter's concurrency cap smooths bursts so Bedrock and OpenSearch see steady load; request a Bedrock quota increase sized to peak passages per minute.
- Set OpenSearch Serverless's maximum indexing and search capacity so a large re-ingest can't run up cost.
- Track queue depth and ingest lag (upload to `ready`) in CloudWatch, and alarm when lag exceeds your target.

## Retrieval

Retrieval casts a wide net (40 passages, hybrid), narrows it with a reranker (top 8), then expands each winner to its whole parent section; this is the evidence set both the answer and the excerpts come from.

### Steps

1. **Hybrid search** for 40 candidates: BM25 on `text` plus k-NN on `embedding`, combined by the `hybrid-norm` search pipeline (min-max normalization, weights 0.3 keyword / 0.7 semantic to start). Filter to active versions so stale passages never appear.
2. **Rerank** the 40 with the Bedrock Rerank API (Amazon Rerank or Cohere Rerank; check which your region offers). A cross-encoder reranker reads query and passage together and is markedly more accurate than vector distance alone.
3. **Drop weak hits** below a relevance threshold (tune on the evaluation set) so the answer is not built on loosely related text. If nothing passes, return "no relevant sections found" rather than a guess.
4. **Expand to sections**: look up each passage's `section_id`, merge passages that share a section, and slice the full section from the canonical text. Keep the matched passage ranges for highlighting.
5. **Fit the budget by dropping whole sections**, lowest-ranked first. Sections are never truncated, so nothing is quoted or summarized out of context.

### Code

```python
import boto3, functools, os

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")
sections_tbl = ddb.Table("sections")
rt = boto3.client("bedrock-agent-runtime")
RERANK_ARN = os.environ["RERANK_MODEL_ARN"]
MIN_SCORE, MAX_WORDS = 0.3, 9000

@functools.lru_cache(maxsize=32)
def canonical(work_id, ver):
    key = f"{work_id}/{ver}.txt"
    return s3.get_object(Bucket="works-text", Key=key)["Body"].read().decode("utf-8")

def search(q, active_versions):
    body = {"size": 40,
            "_source": {"excludes": ["embedding"]},
            "query": {"hybrid": {"queries": [
                {"bool": {"must": {"match": {"text": q}},
                          "filter": {"terms": {"version_id": active_versions}}}},
                {"knn": {"embedding": {"vector": embed(q), "k": 40,
                         "filter": {"terms": {"version_id": active_versions}}}}}]}}}
    res = os_client.search(index="passages", body=body,
                           params={"search_pipeline": "hybrid-norm"})
    return [h["_source"] for h in res["hits"]["hits"]]

def rerank(q, passages, n=8):
    res = rt.rerank(
        queries=[{"type": "TEXT", "textQuery": {"text": q}}],
        sources=[{"type": "INLINE", "inlineDocumentSource": {
                    "type": "TEXT", "textDocument": {"text": p["text"]}}}
                 for p in passages],
        rerankingConfiguration={"type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {"numberOfResults": n,
                "modelConfiguration": {"modelArn": RERANK_ARN}}})
    return [(passages[r["index"]], r["relevanceScore"])
            for r in res["results"] if r["relevanceScore"] >= MIN_SCORE]

def expand(ranked):
    """Group passages by parent section; return whole sections in rank order."""
    evidence, by_section, words = [], {}, 0
    for p, score in ranked:
        sid = p["section_id"]
        if sid in by_section:
            by_section[sid]["hits"].append((p["char_start"], p["char_end"]))
            continue
        sec = sections_tbl.get_item(Key={"section_id": sid})["Item"]
        text = canonical(sec["work_id"], sec["version_id"])[int(sec["char_start"]):int(sec["char_end"])]
        n = len(text.split())
        if words + n > MAX_WORDS:
            continue            # drop the whole section, never truncate it
        words += n
        item = {"cite_id": f"S{len(evidence) + 1}", "section": sec, "text": text,
                "score": score, "hits": [(p["char_start"], p["char_end"])]}
        by_section[sid] = item
        evidence.append(item)
    return evidence
```

The cache holds whole canonical files; for very large works, store byte offsets alongside character offsets and fetch only the section with an S3 ranged GET. The `cite_id` values (`S1`, `S2`…) are short labels for this one response; the durable reference is `section_id` plus `version_id`.

## Source tagging and evidence strength

Citations are rendered from source metadata, not written by the model, and ranking favors current, authoritative sources; the model can choose which sections to cite but cannot invent a source, date, or page.

### Ranking by authority and recency

After reranking, each section's score is adjusted by its source's tags, and superseded editions are removed:

```python
AUTHORITY = {                      # starting weights; tune with the SME
    "practice_guide": 1.00,        # IES/WWC practice guides
    "systematic_review": 1.00,     # meta-analyses, NRP report
    "state_standard": 1.00,        # MS CCRS for ELA, LBPA materials
    "peer_reviewed_study": 0.95,
    "federal_report": 0.90,        # NICHD, NAEP aggregates
    "practitioner_resource": 0.80, # NCIL, FCRR
}

def recency(pub_year, now_year, half_life=10, floor=0.75):
    """Gentle decay; the floor keeps foundational work like the NRP report competitive."""
    return max(floor, 0.5 ** ((now_year - pub_year) / half_life))

def adjust(evidence, works, now_year):
    kept = []
    for e in evidence:
        w = works[e["section"]["work_id"]]
        if w.get("superseded_by"):
            continue
        e["score"] *= AUTHORITY[w["doc_type"]] * recency(int(w["pub_date"][:4]), now_year)
        kept.append(e)
    return sorted(kept, key=lambda e: e["score"], reverse=True)
```

The recency floor matters here: much of the Science of Reading evidence base is decades old and still current, so age should break ties, not bury foundational sources. Questions about Mississippi policy or standards can additionally boost `state_standard` sources.

### Evidence strength flags

Every answer carries one flag: **strong**, **limited**, **mixed**, or **contested**, plus a one-sentence reason. The model proposes it; code then caps it using facts it can check:

- Fewer than two distinct sources cited → at most **limited**.
- Only `practitioner_resource` sources cited → at most **limited**.
- Model reports sources that disagree → **mixed** or **contested**, and the answer must cite both sides.
- WWC practice guides rate each recommendation's evidence (strong, moderate, minimal). Ingestion tags those sections with that rating, and an answer resting on a "minimal" recommendation is flagged **limited**.

### Citation rendering

```python
def cite_label(e, works):
    w, s = works[e["section"]["work_id"]], e["section"]
    where = f"p. {s['page']}" if s.get("page") else s.get("section_path", "")
    return f"{w['publisher']}, {w['pub_date'][:4]}, {where}"
```

The model emits only `S1`, `S2`…; the UI swaps each for this label, e.g. "(IES What Works Clearinghouse, 2016, p. 12)". Every response also shows the advisory-only disclaimer and "Content last updated" with the latest `activated_at` from the `works` table.

## Answer with citations

The LLM sees the evidence sections but returns only structured sentences with section IDs; code validates every citation, and if validation fails the user still gets the excerpts, just without an answer.

### Rules the model follows

- Use only the provided sections; no outside knowledge.
- Every sentence cites one or more section IDs that directly support it.
- Paraphrase; no copied phrases. Verbatim text is shown separately, so a quote inside the answer would blur which text is the source.
- If the sections don't answer the query, say so and mark the result unanswerable.

### Model call (Bedrock Converse, forced tool output)

```python
import boto3, json, os
br = boto3.client("bedrock-runtime")

SYSTEM = ("You answer K-5 teachers' Science of Reading questions. Use ONLY the sections provided. "
          "Every sentence must cite the IDs of the sections that directly support it. "
          "Paraphrase; never copy phrases from the sections. If the sections do not "
          "answer the query, set answerable to false and explain in one sentence. "
          "Rate evidence_strength honestly; if sources disagree, say so and cite each side.")

SCHEMA = {"type": "object", "required": ["answerable", "evidence_strength", "strength_reason", "sentences"], "properties": {
    "answerable": {"type": "boolean"},
    "evidence_strength": {"type": "string", "enum": ["strong", "limited", "mixed", "contested"]},
    "strength_reason": {"type": "string"},
    "sentences": {"type": "array", "items": {
        "type": "object", "required": ["text", "cites"], "properties": {
            "text": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "string", "pattern": "^S[0-9]+$"}}}}}}}

def summarize(query, evidence):
    blocks = "\n\n".join(
        f'<section id="{e["cite_id"]}" work="{e["section"]["work_id"]}">\n{e["text"]}\n</section>'
        for e in evidence)
    resp = br.converse(
        modelId=os.environ["SUMMARY_MODEL_ID"],
        system=[{"text": SYSTEM}],
        messages=[{"role": "user", "content": [{"text": f"{blocks}\n\nQuery: {query}"}]}],
        inferenceConfig={"temperature": 0, "maxTokens": 800},
        toolConfig={"tools": [{"toolSpec": {
                        "name": "record_summary",
                        "description": "Record the cited summary.",
                        "inputSchema": {"json": SCHEMA}}}],
                    "toolChoice": {"tool": {"name": "record_summary"}}})
    content = resp["output"]["message"]["content"]
    return next(b["toolUse"]["input"] for b in content if "toolUse" in b)
```

Forcing a tool call makes the model return JSON matching the schema instead of free text, so citations are fields to check, not patterns to parse.

### Validation

```python
def ngrams(s, n=8):
    w = s.lower().split()
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}

def validate(out, evidence):
    allowed = {e["cite_id"]: e["text"] for e in evidence}
    problems = []
    for i, s in enumerate(out["sentences"]):
        if out["answerable"] and not s["cites"]:
            problems.append(f"sentence {i} has no citation")
        for c in s["cites"]:
            if c not in allowed:
                problems.append(f"sentence {i} cites unknown {c}")
            elif ngrams(s["text"]) & ngrams(allowed[c]):
                problems.append(f"sentence {i} copies text from {c}")
    return problems

def cited_summary(query, evidence):
    for _attempt in range(2):
        out = summarize(query, evidence)
        if not validate(out, evidence):
            return out
    return None   # caller shows excerpts only, with "summary unavailable"
```

**Optional support check.** Validation proves citations point at real sections, not that each section supports its sentence. For a second line of defense, run each sentence and its cited sections through Bedrock Guardrails' contextual grounding check (the `ApplyGuardrail` API) or a separate judge-model call, and drop sentences that score below threshold. This adds latency and cost per query, so measure it on the evaluation set before turning it on.

## Rendering verbatim excerpts

Excerpt text in the response is copied from the canonical file and hash-checked; nothing the model returned is ever placed in an excerpt field.

### Steps

1. **Split the API in two** so excerpts are never held up by the model: `POST /search` returns excerpts in under a second; `POST /answer` takes the same evidence IDs and returns the cited answer. The page shows excerpts first and fills in the answer when it arrives.
2. **Hash-check every excerpt** against the stored `text_sha256` before returning it. A mismatch means the canonical file or index is out of sync: omit that excerpt, log an alert, and don't summarize from it.
3. **Return a durable reference** with each excerpt (`section_id`, `version_id`, `char_start`, `char_end`) so anyone can re-fetch and verify the exact text later.
4. **Render as plain text** with `textContent`, never `innerHTML`, highlighting the matched passages inside the section. Each `[S1]` in the answer links to its excerpt.

### Search handler

```python
import json

def respond(body, code=200):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}

def search_handler(event, _ctx):
    q = json.loads(event["body"])["query"].strip()[:500]
    evidence = expand(rerank(q, search(q, active_versions())))
    excerpts = []
    for e in evidence:
        sec = e["section"]
        if sha(e["text"]) != sec["text_sha256"]:
            log_integrity_failure(sec)          # alert; never show unverified text
            continue
        base = int(sec["char_start"])
        excerpts.append({
            "cite_id": e["cite_id"],
            "work_id": sec["work_id"],
            "section_path": sec.get("section_path"),
            "text": e["text"],                  # straight from the canonical file
            "highlights": sorted([s - base, t - base] for s, t in e["hits"]),
            "ref": {k: sec[k] for k in ("section_id", "version_id", "char_start", "char_end")}})
    if not excerpts:
        return respond({"excerpts": [], "message": "No relevant sections found."})
    return respond({"query": q, "excerpts": excerpts})
```

The `/answer` handler re-loads sections by `ref` (re-checking hashes) rather than trusting excerpt text sent back from the browser, then calls `cited_summary`.

### Frontend rendering

```js
function renderExcerpt(x) {
  const card = document.createElement("article");
  card.id = `ex-${x.cite_id}`;
  const head = document.createElement("h3");
  head.textContent = `[${x.cite_id}] ${x.work_id} › ${x.section_path ?? ""}`;
  const quote = document.createElement("blockquote");
  let pos = 0;
  for (const [s, e] of x.highlights) {
    quote.append(x.text.slice(pos, s));
    const mark = document.createElement("mark");
    mark.textContent = x.text.slice(s, e);
    quote.append(mark);
    pos = Math.max(pos, e);
  }
  quote.append(x.text.slice(pos));
  card.append(head, quote);
  return card;
}

function renderSummary(out, root) {
  for (const s of out.sentences) {
    root.append(s.text + " ");
    for (const c of s.cites) {
      const a = document.createElement("a");
      a.href = `#ex-${c}`;
      a.textContent = `[${c}]`;
      root.append(a, " ");
    }
  }
}
```

The answer is labelled as machine-generated in the UI; the excerpts are labelled as source text with their location, so readers can tell at a glance which is which.

## Phase 2 on the same pipeline

Both Phase 2 capabilities reuse extraction, retrieval, ranking, citation validation, and verbatim rendering unchanged; each adds one data store authored by the SME, one prompt, and one endpoint.

### Lesson Plan Alignment Review (`POST /review`)

1. **Upload** a lesson plan (text, PDF, DOC, DOCX) through the same extractors. Plans go to a separate `plans-temp` bucket with a 24-hour lifecycle delete and are **never indexed** into the corpus. Per scope, the PoC uses synthetic plans only, and the upload screen says so.
2. **Segment** the plan into objectives, activities, and assessment. The model returns character ranges into the plan text, not copied text, so plan quotes are verbatim too.
3. **Score nothing; observe.** For each rubric criterion (five reading components by grade band, plus MS CCRS for ELA standards), retrieve evidence using the criterion and the relevant plan segment, then ask for an observation and a suggestion.
4. **Validate** that every cite is a retrieved section and every plan reference is a valid range, as in Q&A.
5. **Render** observations grouped by component, each with the plan excerpt, the research excerpt, and the suggestion. Language is developmental and about the plan, never a rating of the teacher.

The rubric is data the SME maintains, not prompt text:

```json
{
  "criterion_id": "phonics-k1-explicit",
  "component": "phonics",
  "grade_band": "K-1",
  "look_for": "Explicit, systematic instruction in letter-sound correspondences",
  "standards": ["RF.K.3", "RF.1.3"],
  "anchor_sections": ["wwc-foundational-k3-2016:v2:s0042"]
}
```

Anchor sections are pre-vetted evidence always included for that criterion, alongside what retrieval finds. Standards text is shown verbatim from the ingested MS CCRS document.

### Coaching Scenario Simulation (`POST /scenario`)

1. **Scenarios** are synthetic, SME-reviewed classroom situations stored as JSON (situation, grade, component, anchor sections, reflection prompts) in their own table, not the search index.
2. The teacher reads a scenario and writes how they would respond.
3. Retrieval runs on the scenario plus the response; the model returns feedback with citations and two reflection prompts.
4. **Try another approach:** with no accounts, the browser keeps the attempt history and sends it back, so feedback can compare attempts. Nothing is stored server-side.

This is the natural place to try Bedrock Flows (prompt → Lambda retrieval → prompt), since its steps are mostly model calls.

## Failure handling and evaluation

Every failure degrades toward showing fewer verified excerpts, never toward showing unverified text; accuracy is tracked with a fixed test set run on every change.

### Failure cases

| Case | Handling |
| --- | --- |
| Work re-uploaded | New `version_id` ingests alongside the old one; the switch to `active_version_id` happens only after verification, then old passages are deleted |
| Work deleted from `works-raw` | EventBridge `Object Deleted` starts a cleanup that sets status `deleted`, removes it from active versions, and deletes its passages and sections |
| Ingest fails partway | Step Functions retries the failed step; idempotent `_id`s prevent duplicates; the work stays `ingesting` and is never searched |
| Extraction is poor (scanned, garbled) | Flag works whose text has a high share of non-dictionary words; route to Textract or manual review before activation |
| Excerpt hash mismatch | Omit the excerpt, alert, and exclude it from the answer |
| No hits above the rerank threshold | Return "no relevant sections found" and no answer |
| Answer fails validation twice | Error state: show the excerpts with a "answer couldn't be generated" notice and a retry button; never offered as a mode |
| Embedding model changed | Re-embed into a new index, run the evaluation set, then swap an index alias |

### Evaluation

Build a golden set of 50–100 real queries, each labelled with the sections a domain reader agrees are correct answers. Re-run it whenever chunk size, search weights, the reranker, or any model changes.

| Metric | What it measures | Target to start |
| --- | --- | --- |
| Recall@8 | Share of labelled sections that reach the final evidence set | ≥ 0.85 |
| MRR | How high the first correct section ranks | ≥ 0.7 |
| Citation precision | Share of answer sentences fully supported by their cited sections (human-graded sample) | ≥ 0.95 |
| Unanswerable handling | Share of out-of-scope queries correctly declined | ≥ 0.9 |
| Integrity failures | Hash mismatches in production logs | 0 |

The targets are starting points to adjust once you have a baseline.

**Tooling.** The proposal names Amazon Bedrock evaluation plus SME review. Use Bedrock's RAG evaluation jobs in bring-your-own-response mode: export each golden query's retrieved sections and final answer as a dataset, and have Bedrock score retrieval relevance, correctness, faithfulness, and citation quality. Addison (SME) grades a sample for instructional usefulness and checks evidence-strength flags. The golden set itself is the proposal's "synthetic test question set built with SME input" (week 4).

## Search type: keyword vs semantic vs hybrid

Use hybrid. It catches both exact terms (names, quotes, jargon) and paraphrased meaning, and the reranker then sorts out the merged candidates.

| Search type | How it works | Benefits | Drawbacks | Best for |
| --- | --- | --- | --- | --- |
| Keyword (BM25) | Ranks chunks by shared words, weighted by rarity | Cheap, fast, no ML; exact names and quotes match well; easy to explain | Misses synonyms and paraphrase; sensitive to wording | Known-phrase lookup, legal or code text |
| Semantic (vector) | Embeds query and chunks; returns nearest neighbors by meaning | Finds conceptually related passages; robust to wording | Embedding cost; can miss rare exact terms; results harder to explain | Exploratory questions, themes |
| Hybrid | Runs both and merges normalized scores | Best relevance in practice; covers both failure modes | Two queries per search; weights need tuning | General-purpose search (recommended) |

A keyword-only version of this pipeline drops the embedding step: same pipeline, no `embed()` calls, and a plain `match` query. It is a reasonable first milestone if you want something working in an afternoon.

## Alternatives considered

The managed options are faster to build but can't guarantee exact, versioned, context-complete excerpts, which is the core requirement.

| Option | Benefits | Why not chosen |
| --- | --- | --- |
| Bedrock Knowledge Bases (`RetrieveAndGenerate`) | Hours to build; managed chunking, sync, and generation | Prompt assembly and context trimming are hidden; no control over offsets or verification |
| Knowledge Bases `Retrieve` + our own answer call | Less code than DIY; we still control the answer step | Chunk boundaries and offsets aren't ours, so excerpts can't be pinned to a versioned character range; viable fallback if DIY effort is too high |
| Amazon Kendra | Strong out-of-box relevance, highlighting, access control | Fixed passage boundaries; roughly $800–$1,000+/month idle (approximate) |
| S3 Vectors as the vector store | Near-zero idle cost | No BM25, so exact names and quotes match poorly; keep as an option if keyword accuracy proves less important |
| Aurora PostgreSQL + pgvector | Passages, sections, and works in one database; full-text + vectors | We'd write hybrid merging ourselves; reasonable swap if we'd rather run Postgres than OpenSearch |
| Claude Citations feature (model returns `cited_text` spans) | Spans are extracted from the documents, not generated | Useful as an extra check, but excerpts would still flow through a model response; our offset-and-hash path is the guarantee |

The proposal anticipates **Amazon Bedrock Flows** for multistep workflows. Flows can orchestrate Phase 2's multi-step prompts, but Q&A needs custom code between steps (hash checks, ranking adjustments, citation validation), so Q&A stays in Lambda. A Flow can call these Lambdas later if the Hub wants a Flows-based demo.

## Security, cost, and next steps

### Security

- **Storage:** both buckets private with versioning on; `works-text` is write-once for the ingest role (deny `DeleteObject` and overwrites except from the cleanup workflow).
- **IAM:** least privilege per Lambda; search reads the index, sections, and canonical text and calls the embed and rerank models; only `/answer` can call the answer model.
- **Abuse and bots (public app):** AWS WAF in front of CloudFront and API Gateway with per-IP rate-based rules, and optionally an IP allowlist of school networks for the PoC; add Bot Control and CAPTCHA challenges if it opens to the public. API Gateway throttling as a second limit.
- **Hard spend caps:** reserved concurrency on `/answer` bounds simultaneous model calls; AWS Budgets alerts on Bedrock spend; a daily spend breaker switches searches to the error state rather than running up cost.
- **Response cache:** cache full responses keyed by normalized query plus a corpus generation number that increments on each activation, with a short TTL (about an hour). Popular queries then cost one answer, not thousands.
- **Scraping:** cap results per query and excerpts per IP per day, to limit load and cost from automated harvesting (sources are public or openly licensed, and citations provide the attribution licenses like CC BY require).
- **Untrusted input:** both queries and work text can carry instructions to the LLM. The system prompt, forced tool output, and validation keep them from changing the response, and excerpts never pass through the model.
- **Web:** CORS limited to the CloudFront domain, query length capped at 500 characters, excerpts rendered with `textContent` only.
- **Monitoring:** CloudTrail plus CloudWatch alarms on integrity failures, WAF blocks, ingest lag, and Bedrock spend.

### Cost drivers (approximate)

| Component | Driver | Note |
| --- | --- | --- |
| OpenSearch Serverless | Fixed minimum capacity | Largest idle cost, roughly $175+/month at the smallest dev setting |
| Bedrock embeddings | Tokens embedded at ingest, plus one embedding per query | One-time per corpus; re-paid only on model change |
| Bedrock Rerank | Per query (40 candidates) | Scales with traffic |
| Answer model | Input tokens (up to \~9,000 words of evidence) per answer | Largest per-query cost; runs on every search, so the response cache matters most here |
| Lambda, Step Functions, DynamoDB, S3 | Requests and storage | Small at prototype scale |

Price this in the [AWS Pricing Calculator](https://calculator.aws/) with your corpus size and expected queries per day.

### Build plan by week

This maps the design onto the proposal's timeline; Q&A is complete and validated by the end of week 6, and Phase 2 uses what's left.

**Week 2 (Oct 5): foundations**

- [ ] Bedrock model access: Titan Text Embeddings V2, a rerank model, and the answer model
- [ ] OpenSearch Serverless collection, `passages` index, and `hybrid-norm` pipeline; confirm hybrid search works on it
- [ ] License review and `meta.json` sidecars for the core Q&A sources (WWC guides, NRP/NICHD, MS CCRS for ELA, LBPA materials)

**Week 3 (Oct 12): ingestion**

- [ ] Extractors for text, PDF (with Textract fallback), DOCX, and DOC
- [ ] Canonical text, chunker with offset assertions, embedding cache, Step Functions workflow, license gate
- [ ] Ingest the core sources and spot-check offsets on 3–5 of them

**Week 4 (Oct 19): Q&A end to end**

- [ ] `/search`: hybrid search, rerank, authority and recency adjustment, section expansion, hash-checked excerpts
- [ ] `/answer`: cited answer, evidence-strength flag, validation
- [ ] Golden question set with Addison; baseline metrics and a first Bedrock evaluation job

**Week 5 (Oct 26): harden and expose**

- [ ] Tune evidence-strength flags against SME judgment
- [ ] Expand the corpus (ERIC, PMC Open Access, NCIL, FCRR); add an HTML extractor if those sources need it
- [ ] Teacher-facing page: excerpts first, streamed answer, citation labels, disclaimer, last-updated date
- [ ] WAF rules, throttling, spend caps, response cache; first usability pass

**Week 6 (Nov 2): Phase 1 complete**

- [ ] SME accuracy and usability testing against the evaluation targets
- [ ] Begin `/review`: rubric data and lesson-plan segmentation

**Week 7 (Nov 9): Phase 2 and closeout**

- [ ] Final Q&A tuning with district SMEs
- [ ] Finish `/review`; build `/scenario` as far as time allows
- [ ] Closeout documents and GitHub repo

**Week 8 (Nov 16):** Showcase demo of the validated Q&A plus Phase 2 progress.

### Open questions

- How large is the corpus (number of works, total size), and how many uploads per day at peak? This sizes Bedrock quotas, OpenSearch capacity, and cost.
- What share of PDFs are scanned? Textract adds cost and ingest time per page.
