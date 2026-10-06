# Progress log

Newest first. Each entry: what was done, how it was verified, and what's next.

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
