# Progress log

Newest first. Each entry: what was done, how it was verified, and what's next.

## 2026-10-07 — Session 2f: material review feature

**Done (D56-D61)**: `src/read/review.py` (goal inference, per-question review, validation,
retry with feedback, verified quotes from material and research), `TempStore` (24-hour purge),
PowerPoint extraction, `rubric/checklist.json` (10 questions), API routes, `web/review.html`
(upload or paste, confirm goal, feedback grouped by component with expandable material and
research references), `web/checklist.html` (edit questions). 121 unit tests pass.

**The review checklist (`rubric/checklist.json`) is a DEVELOPMENT PLACEHOLDER.** Its questions were drafted by Claude only so the feature could be built and tested; Addison Robertson (SME) must rewrite them before any teacher use.

**Verified live** (synthetic sample lesson I wrote, about 25 cents for two runs): goal inferred as
kindergarten / phonemic awareness / segmenting words; 10 questions in about 20 s. First run:
"seen" badges contradicted the text (fixed in the prompt); second run: badges match (no stated
objective -> no; phonological awareness -> yes; coloring worksheet as assessment -> no). One
question fails per run on the uncited-reuse rule (different question each run).

## 2026-10-07 — Session 2e: verified quotes; answer at the top

**Why answers regenerated**: Nova's first drafts failed the 8-word copy rule on 8 of 10
questions; the copied text was mostly names of WWC recommendations and MS standards. A stricter
prompt (4 words) didn't help (3/10).

**Done**: D54 verified quotes (code finds reused wording and shows it as a quotation sliced from
the source); D55 answer under the question box. CLAUDE.md rule 4 and design (R11) updated.
New module `src/read/quote.py`. 106 unit tests pass.

**Tried and dropped**: model-marked quotes (a `quote` field in the tool schema): 8/10 failed
because Nova didn't mark them consistently.

**Verified live (10 questions, about 25 cents for this round)**: 10/10 answered on the first
attempt, about 3 s each, 50 verified quotes shown. Found and fixed: a displayed quote dropped
a real hyphen ("instructional-level"); line-break hyphens are now always kept.

## 2026-10-07 — Session 2d: NotebookLM-style answers

**Done (D53)**: Test page now shows the answer first with numbered citation chips; a chip opens
the verbatim excerpt (matched passage highlighted) under the answer; the Sources list below has
cited sources first (expandable) and uncited ones collapsed. Declines label the list "Closest
matches". CLAUDE.md and design.md (R10) updated. This also closes the earlier open item about
off-topic questions showing irrelevant excerpts.

**Verified**: in Chrome via page script: "How should I teach phonemic awareness in kindergarten?"
-> 8 sources, answer with 7 chips; clicking chip 1 opened one reference card with a highlighted
excerpt. Screenshots timed out (browser tool issue), so the visual check is John's.

## 2026-10-07 — Session 2c: search options

**Done**: Test page gets a grade range (Pre-K to 12) and max results (1-15), plus a collapsed
"Advanced search options" panel (minimum relevance score, candidate pool, keyword vs. meaning
weight, reranker on/off). Excerpt cards show the source's grade band. Debug shows the options
used, sources in the grade range, and how many passages fell below the minimum score.

**Verified**: 96 unit tests pass. Live API on the 3 active sources: grades 4-5 drops WWC (K-3);
max results 3 returns 3; reranker off changes order; keyword weight 0.9 works through a one-off
pipeline; min score 0.5 removes everything with the reranker on (scores are tiny) but keeps 2
with it off. Controls checked in Chrome (dark mode).

## 2026-10-07 — Session 2b: answers failing the copy check on real research

**Found**: John hit "answer couldn't be generated" (Nova copied 8-word phrases from 3 sections).
Root cause beyond Nova's habit: the retry repeated the identical temperature-0 request, so it
could never succeed. John signed off WWC, NRP, MS CCRS 2025, and 2016 (superseded) in the GUI.

**Fixed (D49)**: the retry now includes the rejected sentences and the quoted copied stretches.
Debug panel shows every attempt. 91 unit tests pass; dev server restarted.

**Verified live (8 real teacher questions, about 8 cents)**: 3 passed first try, 4 rescued by
the retry, 1 declined ("How often should I assess progress?"; may be a real gap in these
sources), 0 failed. Answer time 2-9 s including search.

## 2026-10-07 — Session 2a: local web GUI

**Done**
- `scripts/dev_server.py` (FastAPI, localhost only) + `web/index.html` (Test page: teacher view
  + debug) + `web/sources.html` (upload/ingest with live progress, list/status, license
  evidence + sign-off, expiry/superseded editing, deactivate/reactivate, delete with typed
  confirmation, paged section browser with hash checks) + `web/style.css` (light/dark).
- Shared code: `src/read/service.py` (search/answer), `src/read/pipeline.py` (ingest,
  activate, status, tags, delete). `scripts/ingest.py` now calls the pipeline.
- License research for the placeholders written into each meta.json (evidence + recommendation);
  sign-off left for John.

**Verified**
- 87 unit tests pass (new: /answer ignores browser text, omits tampered/wrong-version/expired
  sections, decline message, error state carries a flag, display status).
- In Chrome: Sources page lists the 4 placeholders (awaiting_license), detail panel shows
  license evidence, section browser pages through 320 WWC sections with hash checks.
- Full flow with a self-written test fixture (not research): upload -> ingest -> awaiting_license
  -> sign-off -> ready -> Test page question -> 2 excerpts with highlights and labels ->
  cited Nova answer -> flag "limited" (single practitioner source) -> debug timings (cold embed
  3.4 s, search 0.15 s, rerank 1.6 s). Fixture then deleted from search and tables.

**Next**: John reviews and signs off licenses in the GUI (Sources page); then real questions.

## 2026-10-06 — Session 1i: local DynamoDB + ingestion script

**Done**
- DynamoDB Local added to docker-compose (D40). `store.py`: `dynamodb_resource`,
  `ensure_tables`, write-once `LocalTextStore`. `src/read/ingest.py`: `check_meta`,
  `license_ok`, `local_version_id`, `work_record`. `scripts/ingest.py --local`: the full ingest
  path (metadata check, version ID, extract, canonical text, chunk with verified offsets,
  embed with retries, bulk index, sections to DynamoDB, count check, license gate, activation,
  retiring the previous version).
- Ingested all 4 placeholders (embedding cost about $0.005 total).

**Verified**
- 82 unit tests pass.
- Local stores: 4 works rows (all awaiting_license; MS 2016 superseded_by MS 2025), 1,466
  sections in DynamoDB, 1,729 passages in OpenSearch; active (searchable) versions: none,
  as the license gate requires. Re-running an unchanged file prints "unchanged".

**Next**: someone verifies the placeholder licenses, then activation and the full search path
run on real reading research; after that, the `/search` and `/answer` handlers.

## 2026-10-06 — Session 1h: placeholder corpus + real-document fixes

**Done**
- Downloaded 4 public placeholders into `corpus/` (PDFs git-ignored) with draft `.meta.json`
  sidecars (D39): WWC foundational skills 2016, NRP report 2000 (ERIC), MS CCRS ELA 2025 and 2016.
- Real documents exposed 5 problems, all fixed (D35-D38): cover letters as headings; TOC lines;
  9-word sections in the standards; two-column pages merged; duplicated ligatures and run-together
  words in the old NRP scan.

**Verified**
- 76 unit tests pass. Corpus summary after the fixes:
  WWC 123 pp: 320 sections (median 98 words); NRP 37 pp: 72 sections (127 words);
  MS 2025 284 pp: 553 sections (89 words); MS 2016 229 pp: 521 sections (89 words).
  Extraction takes about 8-43 s per document.
- Spot check: WWC Recommendation 1 now reads in order with correct page labels.

**Known gaps**: figures/boxed tables still jumble; the NRP cover page is garbled OCR.

## 2026-10-06 — Session 1g: copy fix + reranker cut-off removed

- Prompt now states the 8-word rule exactly (D34). Before: Nova copied "...against the stored
  text_sha256 before returning it" on 2 of 2 attempts. After: 3 of 3 test questions passed on
  the first attempt, with accurate paraphrases.
- Reranker cut-off removed (D33): Amazon Rerank scores relevant sections 0.0001-0.0023, so the
  0.01 floor wrongly declined real questions. Top 8 kept in rerank order; Nova declines
  off-topic questions itself (verified: "long division" declined with no sentences).
- Live re-test passed; 72 unit tests pass. Session AWS cost so far: under $0.15.

## 2026-10-06 — Session 1f: Q13 rerank cut-off

- Implemented D32 (keep top 8 by rerank order; "no relevant sections" when best score < 0.01).
- Live re-run: 8 relevant sections kept (Overview, Rendering verbatim excerpts, Goals, Answer
  with citations...); off-topic question still declined with no answer call.
- Nova Pro copied an 8-word phrase on both attempts, so the validator blocked the answer
  (error state shown). Rule 4 is working; answer quality needs prompt tuning (see
  open-questions "To watch").
- 71 unit tests pass.

## 2026-10-06 — Session 1e: reranker live (Amazon Rerank 1.0, us-west-2)

**Done**
- `rerank()` in retrieve.py (top 8 of 40, cut-off parameter); IAM `read-poc-developer`
  updated by John: us-west-2 allows only `bedrock:Rerank` + `InvokeModel` on
  `amazon.rerank-v1:0`; Titan/Nova pinned to us-east-1. The boundary policy update is deferred
  (it doesn't exist yet and isn't needed until Lambdas deploy).

**Verified (live, cost about 2 cents)**
- Amazon Rerank 1.0 is ACTIVE in us-west-2. Region lock: us-west-2 OpenSearch/S3/Titan denied,
  eu-west-1 denied, us-east-1 unaffected.
- Full chain on the design-doc test corpus: local hybrid search -> rerank -> sections -> hash
  check -> Nova Pro answer with valid citation (search+rerank about 1 s warm, 3-5 s cold).
  An off-topic question returns "No relevant sections found" with no answer call.
- Finding: Amazon Rerank scores are nearly binary; the 0.3 cut-off drops good sections (Q13).
- 71 unit tests pass.

## 2026-10-06 — Session 1d: John's answers to Q1–Q11 implemented

**Done**
- Q1/Q5: PDF paragraphs + headings rebuilt from layout (D20, D21).
- Q3: optional `expires_on`; `is_active()` / `active_versions()` (D25).
- Q4: sections without headings cap at 5 paragraphs / 500 words; headed continuations keep
  their heading (D22). Q7: page ranges (D24).
- Q11: local OpenSearch 2.19.1 in Docker (`docker-compose.yml`), `store.py`, `embed.py`,
  `scripts/setup_opensearch.py --local`.
- Spend cap ($10/month without explicit OK) added to CLAUDE.md and memory (D30).
- design.md: "PoC revisions" R1–R7 + `expires_on` in the works table (D28).

**Verified**
- 69 unit tests pass; ruff clean.
- Design-doc PDF through the new extractor: 237 paragraphs, 23 correct headings, 25 named
  sections, page ranges right, running header and page numbers gone.
- Local end-to-end: 39 passages embedded with Titan (about $0.0002) and indexed; hybrid query
  through `hybrid-norm` returns the right sections first; the version filter returns 0 hits
  for an inactive version; excerpts hash-check and get page-range labels.

**Open**: Q2 (reranker vs. AWS-model rule), Q12 (proposal doc), source files, AWS test setup.

## 2026-10-06 — Session 1c: answer model live check (Nova Pro)

**Done**
- John set the requirement: the answer model must be an Amazon model. Nova Pro chosen (D19);
  `read-poc-developer` updated by John to allow `amazon.nova-pro-v1:0`.
- Live smoke test with `ClaudeAgent` (synthetic test passages, not corpus text):
  Titan V2 embedding returned 1024 dims, norm 1.0; Nova Pro accepted `temperature=0` + forced
  `record_answer` tool, ~1 s per call. Answerable question passed `validate()`; unanswerable
  question was correctly declined.
- Bug found and fixed: `validate()` rejected a decline with zero sentences, which would have
  turned every correct decline into the error state. Now allowed when `answerable` is false
  (D13 updated). 55 tests pass.

**Observed, worth watching**
- Nova's paraphrase stayed close to the source (e.g. "hear, separate, and combine sounds in
  spoken words" vs. "...combine the sounds in spoken words"). It passed the 8-gram rule only
  because one word differed. The rule is working as specified, but Addison may want to judge
  whether near-copies are acceptable (golden-set review).

## 2026-10-06 — Session 1b: AWS credential for the agent

**Done**
- IAM policies `read-poc-boundary` and `read-poc-developer` written to `infra/iam/` (account ID
  filled in; not committed yet, waiting on John) and created by John in the console.
- IAM user `ClaudeAgent` (account ACCOUNT_ID) with `read-poc-developer`; CLI profile
  `read-poc` (us-east-1) configured from John's downloaded key CSV. The secret was never
  printed or sent to chat.

**Verified (read-only)**
- `sts get-caller-identity` returns `user/ClaudeAgent`.
- Allowed: Bedrock list models (21 Amazon models), Titan Text Embeddings V2 `ACTIVE` in
  us-east-1, OpenSearch Serverless list collections (none exist, so nothing is billing yet).
- Denied as intended: Bedrock in us-west-2 (region lock), S3 list-all-buckets, IAM list-users,
  EC2 describe. So no broader policy is attached to the user.

**Next**
- Pick the answer model (`ANSWER_MODEL_ID`), enable model access, and update the
  `ANSWER_MODEL` line in `read-poc-developer`.
- Monthly AWS Budget alert (console, admin).
- Answer Q1–Q5 in `docs/open-questions.md`.

## 2026-10-06 — Session 1: repo bootstrap and core library (week 2)

**Done**
- Repo created at `C:\Users\John\source\repos\read` (local git, `main`). `CLAUDE.md` and
  `docs/design.md` copied from John's Downloads.
- Toolchain installed: Python 3.12.10, AWS CLI 2.37.9, SAM CLI 1.166.2 (via winget). Project
  venv `.venv` with the pinned `requirements.txt` + `requirements-dev.txt`.
- `src/read/chunk.py`: sections/passages with exact offsets, sentence splitting for paragraphs
  over 400 words, page numbers via bisect, `verify()` that fails ingestion on any mismatch.
- `src/read/extract.py`: content sniffing; txt (UTF-8 first, then charset-normalizer),
  text-layer PDF with page offsets, DOCX with heading styles and tables in document order.
  Scanned PDFs and `.doc` are rejected with a clear message (Textract and LibreOffice deferred).
- `src/read/cite.py`: labels from metadata only; "Content last updated".
- `src/read/answer.py`: forced `record_answer` tool call, `validate()`, evidence-strength
  caps, `cited_answer()` with one retry and an error state.
- `src/read/retrieve.py`: hybrid query body, authority/recency `adjust()`, `expand()` with a
  whole-section budget, `verified()` hash check with integrity logging, excerpt shape.

**Verified**
- `pytest`: 53 passed. `ruff check` and `ruff format --check` clean.
- Coverage of the CLAUDE.md testing priorities: chunker (Unicode, very long paragraphs,
  no headings, page offsets, tamper detection); validator (unknown cite, missing cite,
  copied 8-gram, unanswerable, malformed output, retry, error state); hash check (tampered
  section omitted and logged); labels only from metadata; ranking (superseded excluded,
  recency floor).
- Smoke test: real pdfplumber + chunker on the design-doc PDF. This found the Q1 issue
  (one paragraph per PDF page).

**Not done / blocked**
- AWS work (week-2 checklist): no `read-poc` profile on this machine yet; Bedrock model
  access, the OpenSearch collection and the hybrid-norm check all wait on it.
- `embed.py`, `store.py`, handlers, `template.yaml`, `scripts/*`, `web/index.html`: not
  started. They depend on Q2/Q3 and on AWS access.
- 10 open questions in `docs/open-questions.md`; 17 provisional decisions in `docs/decisions.md`.

**Next (once Q1–Q5 are answered and the profile exists)**
1. PDF paragraph reconstruction (if Q1 = A) and heading patterns (Q5) on 3–5 real sources.
2. `scripts/setup_opensearch.py` + `template.yaml` (shown to John before any deploy).
3. `embed.py`, `store.py`, `scripts/ingest.py` with the license gate.
