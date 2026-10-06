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

## Open

### Q2. Reranker in this build
It can be in this build. The blockers are:
- **The AWS-model requirement.** The only reranking model offered in us-east-1 is Cohere
  Rerank 3.5 (not made by Amazon). Amazon's own reranker (Amazon Rerank 1.0) isn't offered in
  us-east-1; it is in some other regions, which the account's region lock blocks.
- **Cost:** small, priced per query (check the Bedrock pricing page for the current rate).
- **Work:** about half a day (call, threshold, IAM permission `bedrock:Rerank`).

Options: (A) allow Cohere Rerank 3.5 as an exception to the AWS-model rule; (B) call Amazon
Rerank 1.0 in another region (loosen the region lock for that one action); (C) no reranker,
hybrid scores with a threshold tuned on the golden set.

### Q12. The Hub/MDE proposal document
D28 makes the proposal the ground truth for the final product, but it isn't in the repo.
Add it as `docs/proposal.pdf` (or .docx) so requirements can be checked against it.

## Needs John (not decisions)
- First source files (WWC foundational skills guide 2016, NRP "Teaching Children to Read"
  2000, MS CCRS for ELA): save to `corpus/` and send the download URLs.
- AWS OpenSearch Serverless test: John sets it up, then says go.
