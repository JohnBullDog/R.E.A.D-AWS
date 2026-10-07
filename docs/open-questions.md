# Open questions

Answered questions move to `docs/decisions.md` (D-numbers in brackets).

## Answered 2026-10-06
- Q1 PDF paragraphs: A, rebuild from layout [D20]
- Q3 Superseded/ordering: optional expiration date + versioning; inactive works filtered before search [D25]
- Q4 Sections without headings: B, 5 paragraphs / 500 words [D22]
- Q5 Heading styles: vary, not under our control, so formatting-based detection [D21]
- Q6 Single-level section_path: OK for now [D23]
- Q7 Page ranges: yes [D24]
- Q8 Evidence-strength details: deferred until after the PoC [D26]
- Q9 Recency curve: keep [D27]
- Q10 design.md: update it; the proposal is ground truth for the final product [D28]
- Q11 Development search: local Docker; John sets up the AWS test first [D29]
- Q2 Reranker: in this build, Amazon Rerank 1.0 in us-west-2 [D31]
- Q13 Rerank cut-off: order only, top 8, no cut-off; the answer model declines [D33]

## Open

### Q12. The Hub/MDE proposal document
D28 makes the proposal the ground truth for the final product, but it isn't in the repo.
Add it as `docs/proposal.pdf` (or .docx) so requirements can be checked against it.

## To watch (no decision yet)
- The reranker barely beat hybrid order on the design-doc test corpus. Compare with and
  without it on the golden set before keeping it.

- Figures and boxed tables in PDFs (e.g. WWC 'Example 1.1') still extract as jumbled text.
  Options later: detect and skip figure regions, or keep them out of excerpts.
- The NRP placeholder is the ERIC scan of the summary report; its cover page text is garbled
  (old OCR). The body text is fine.

- Grade tags are per source, so a K-12 source (NRP, MS standards) matches every grade range.
  If grade filtering matters for teachers, sections would need their own grade tags (e.g.
  the MS standards have a grade per section).

- Quote-heavy answers: with D54, some answers are mostly quotes (one had 6 quotes) and some
  quotes are fragments where Nova changed one word mid-phrase. Option: if more than about half
  of an answer's words are quoted, retry asking for more of its own words. Needs John's call.

- Review checklist: DEVELOPMENT PLACEHOLDER. Addison must rewrite every question in
  `rubric/checklist.json` (Checklist page) before teacher use, and decide which questions are
  whole-document, which are core, and each combine rule.
- Review v2 tuning on real samples: triage thresholds (D64); the 'all' combine rule is strict
  (a family letter in a unit pulled 'activities work toward the goal' to partly).
- Review: the Mississippi-standards question sometimes fails validation (Nova reuses research
  wording without citing that section, on both attempts), and nothing yet checks that a
  suggestion is actually supported by the section it cites. Options later: auto-add the
  citation for reused wording; a support check on the golden set.

- Review v2 whole-document verdicts are unreliable (digraph-unit test, 2026-10-07): goal leaks
  into the material, 'any' combine rule, no material citation required, sectioning ignores
  heading levels. Five fixes proposed in `docs/handoff.md`; need John's go-ahead.

## Needs John (not decisions)
- License verification for the 4 placeholders (John or Addison): check each source's terms,
  then fill in `license`, `license_verified_by` (your name) and `license_verified_on`
  (YYYY-MM-DD) in each `corpus/*.meta.json` and re-run ingest. Until then nothing is searchable.
- Boundary policy `read-poc-boundary`: not needed until we deploy Lambdas (week 4); I'll walk
  John through creating it then.
- Real sources: placeholders are in `corpus/` for now (D39); the real list comes from Addison.
- AWS OpenSearch Serverless test: John sets it up, then says go.
