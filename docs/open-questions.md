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

## Open

### Q13. Rerank cut-off for Amazon Rerank
The design's 0.3 cut-off assumed a reranker whose scores spread out. Amazon Rerank 1.0's scores
are nearly all-or-nothing. Live test on the design-doc test corpus:
- "How does the system make sure an excerpt is the exact source text?": scores 0.324 (Overview),
  0.002 (Rendering verbatim excerpts, the best section), 0.002, 0.002, 0.000... With 0.3, only
  one section survives, so the answer was thin and flagged "limited".
- "What is the best way to teach long division?" (off-topic): every score 0.000.

The ranking order is good; the absolute numbers are not comparable to 0.3.

Options:
- **A (recommended):** use the reranker for order only and keep the top 8. Decide "no relevant
  sections found" from the top score alone (start at 0.01; Addison's golden questions set it).
- **B:** keep a per-section cut-off but lower it (e.g. 0.001) and tune later.
- **C:** keep 0.3.

Caveat: the test corpus is our own design doc, not reading research; real sources may score
differently. The golden set (week 4) is the real test either way.

### Q12. The Hub/MDE proposal document
D28 makes the proposal the ground truth for the final product, but it isn't in the repo.
Add it as `docs/proposal.pdf` (or .docx) so requirements can be checked against it.

## Needs John (not decisions)
- Boundary policy `read-poc-boundary`: not needed until we deploy Lambdas (week 4); I'll walk
  John through creating it then.
- First source files (WWC foundational skills guide 2016, NRP "Teaching Children to Read"
  2000, MS CCRS for ELA): save to `corpus/` and send the download URLs.
- AWS OpenSearch Serverless test: John sets it up, then says go.
