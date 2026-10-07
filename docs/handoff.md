# Hand-off — end of session 2 (2026-10-07)

Read this first next session, then `docs/progress.md` (newest entry) and `docs/open-questions.md`.

## Where things stand

| Area | State |
|---|---|
| Q&A (`/search`, `/answer`) | Works end to end locally: hybrid search (OpenSearch in Docker), Amazon Rerank 1.0 (us-west-2), Nova Pro answer with verified quotes, NotebookLM-style page. |
| Sources | Ingest + manage from the Sources page. 4 placeholder sources in `corpus/` (local use only, not in git). |
| Material review v2 | Built and committed (`228fa48`). Sections, triage, whole-document checks, cited summary. **Runs cleanly but its whole-document verdicts are not trustworthy yet** — see "Review test results" below. |
| Checklist | `rubric/checklist.json` is a **DEVELOPMENT PLACEHOLDER**. Addison must rewrite every question and decide scope / core / combine rules. |
| Tests | 136 unit tests pass (`pytest`). |
| Git | `main` is **1 commit ahead of GitHub** (`228fa48`, review v2) — not pushed. Run the leak check (no account ID, no `corpus/`, no `data/`, no personal email, no co-author lines) before pushing. This file and the progress / open-questions edits from this hand-off are **uncommitted**. |
| AWS | Nothing deployed. No OpenSearch Serverless collection exists, so nothing bills while idle. Only Bedrock calls cost money (this session's review runs: roughly $1 total). |

## How to start it up

```bash
cd C:\Users\John\source\repos\read
docker compose up -d                  # local OpenSearch :9200 + DynamoDB Local :8000
.venv\Scripts\python scripts\dev_server.py   # http://127.0.0.1:8080  (Test, Sources, Review, Checklist)
```

AWS profile `read-poc` (IAM user ClaudeAgent) is used for Bedrock. Docker containers
`read-opensearch-1` and `read-dynamodb-1` were left running; stop with `docker compose stop`
if you need the RAM.

## Review test results (2026-10-07, last thing done)

Test file: `C:\Users\John\Downloads\read-review-sample-grade1-digraphs.docx` (synthetic 1st-grade
unit, "Digraph Detectives: sh, ch, th", 5 days, 1,570 words, made by
`scratchpad/make_docx.py`). Goal used: *"1st grade: students read and spell one-syllable words
with the digraphs sh, ch, and th, at the beginning and end of words."*
Result stored in `data/plans-temp/2257344361c3442b/review.json` (auto-deleted after 24 h).

Run: 15/15 sections and 7/7 whole-document checks valid first try, 26 calls, 78 s.

| Planted in the unit | Expected | Got |
|---|---|---|
| No learning objective stated | none / partly | **met** ❌ |
| No real check of learning | weak | **met** ❌ |
| Thin support for struggling readers | partly | **met** ❌ |
| Off-goal family letter / badge ceremony | not penalized | ✅ |
| Explicit modeling (Day 1) | praised | ✅ |
| Corrective feedback ("sop" → "shop") | yes | only partly ⚠️ |
| Word chaining, decodable story | praised | ✅ |
| Word ladders (Day 4 chaining) | praised | no findings ❌ |
| Mississippi standards | named | RF.1.3a / RF.1.3b, grounded in research ✅ |

The summary missed the missing objective and missing assessment, and one takeaway was wrong
(asked for digraphs at the beginning and end of words — Day 4 does exactly that).

### Root causes found

1. **Goal leaks into the material.** The section prompt includes the teacher's goal and Nova
   reports it as stated by the material ("The material clearly states that students will read
   and spell…" — that's the teacher's wording, not the unit's).
2. **The `any` combine rule** (objective, check-learning, support) lets one false "full" make
   the verdict "met", and code forces the combine call to that verdict, even when its own notes
   say "could be more explicit".
3. **Whole-document answers need no material citation**, so a "full" can rest on nothing.
4. Observations restate the question instead of naming evidence.
5. Nova puts whole-document questions into section findings (sanitizer drops them; happened in
   6 of 15 sections — prompt confusion).
6. **Sectioning ignores Word heading levels**: 15 sections instead of ~9; parent headings
   ("Day 1") missing from titles; one merge crossed a day boundary ("Badge ceremony / Day 4…").

### Proposed fixes (waiting on John's OK — none started)

1. Label the goal in the section prompt as "the teacher's goal, not part of the material"; ask
   "does the material itself state…".
2. A whole-document "full"/"partial" answer must cite the material part it relies on; otherwise
   code downgrades it.
3. Combine rule for objective / check-learning / support: `any` → `judge` (checklist rule
   change — Addison should own it eventually).
4. Group sections by top-level heading, include the parent heading in titles, never merge
   across a top-level heading.
5. Observations must name specific evidence from the material.

Touches `src/read/review.py`, `rubric/checklist.json`, `tests/test_review.py`,
`docs/decisions.md`. Re-test on the same file: about 15–20¢ per run.

## Open items (carry-over)

- **Push `228fa48`** after a leak check (John to say when).
- License sign-off for the 4 placeholder sources (John/Addison fill `license_verified_by/on`).
- Addison: rewrite the review checklist; golden questions (`eval/golden.jsonl`) + eval script.
- Disclaimer and decline wording review (D46).
- Quote-balance rule for quote-heavy answers (needs John's call).
- Triage threshold tuning (D64) and the strict `all` combine rule.
- Per-section grade tags (K-12 sources match every grade range).
- Q12: add the Hub/MDE proposal document to the repo.
- AWS deployment (week 4): `template.yaml`, Lambdas, S3, `read-poc-boundary` policy (not yet
  created in IAM), short OpenSearch Serverless test — John sets it up and approves the cost
  first ($10/month cap).

## Standing rules for whoever picks this up

- Never add Claude / Anthropic / VS Code co-author or attribution lines to commits; commits use
  the GitHub no-reply email (repo-local config).
- Public repo = code only: never commit `corpus/`, `data/`, the proposal/contract, or the real
  IAM files with the account ID.
- Never request, store, or print the AWS secret key.
- Ask before any AWS cost beyond the $10/month cap, any `sam deploy`, IAM change, or billable
  resource.
- Review material is synthetic only (rule 7); plans-temp auto-deletes after 24 h.
- Run design / structure decisions by John before building; log choices in `docs/decisions.md`.
