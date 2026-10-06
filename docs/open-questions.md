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
- Q13 Rerank cut-off: order only, top 8, best-score floor 0.01 [D32]

## Open

### Q12. The Hub/MDE proposal document
D28 makes the proposal the ground truth for the final product, but it isn't in the repo.
Add it as `docs/proposal.pdf` (or .docx) so requirements can be checked against it.

## To watch (no decision yet)
- Nova Pro copies source phrases: with 8 sections of evidence it copied an 8-word phrase on
  both attempts, so the validator correctly blocked the answer (error state). Fix belongs
  in golden-set tuning: e.g. state the 8-word rule in the prompt, or compare Nova Premier.
  Any prompt change will be shown to John first.

## Needs John (not decisions)
- Boundary policy `read-poc-boundary`: not needed until we deploy Lambdas (week 4); I'll walk
  John through creating it then.
- First source files (WWC foundational skills guide 2016, NRP "Teaching Children to Read"
  2000, MS CCRS for ELA): save to `corpus/` and send the download URLs.
- AWS OpenSearch Serverless test: John sets it up, then says go.
