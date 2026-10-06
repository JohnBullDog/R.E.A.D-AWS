# Open questions (need John's decision)

Each item lists the options and my recommendation. Where code already exists, the
"Current code" line says what it does today so nothing is hidden. Answer by ID (e.g. "Q1: B").

## Blocking or high impact

### Q1. PDF paragraphs and headings (blocks week 3 ingestion quality)
pdfplumber's `extract_text()` separates lines with single `\n`, and the chunker splits paragraphs
on blank lines, so **each PDF page becomes one paragraph**. A smoke test on the design-doc PDF
(28 pages) gave 28 paragraphs, 28 passages and 5 sections, with no headings detected (every
`section_path` empty). Excerpts would start and end at page breaks, often mid-sentence.
- A. Rebuild paragraphs from pdfplumber word/line geometry: a blank line wherever the
  vertical gap or indentation jumps; headings from font size or bold. More code, but it's
  the only option that gives real sections. **(Recommended)**
- B. Keep pages as paragraphs and rely on sentence splitting only. Simple, but sections are
  arbitrary 1500-word page runs.
- C. Convert PDFs to DOCX outside the pipeline before ingest. Moves the problem to a manual step.

Current code: B (design-doc behavior).

### Q2. Retrieval without the reranker
CLAUDE.md defers the reranker to week 4, but the design's threshold (`MIN_SCORE = 0.3`) and
top-8 cut are on **rerank** scores. Without a reranker, what decides "drop weak hits" and
"no relevant sections found"?
- A. Take the top 8 hybrid hits with a min-max normalized score threshold tuned on the golden
  set; start with no threshold and log scores. **(Recommended)**
- B. Pull the reranker forward into week 2/3 so the design's thresholds apply as written.

Current code: nothing built yet; `expand()` accepts any ranked list.

### Q3. Order of authority/recency adjustment vs. section expansion
The design runs `expand()` (which spends the 9,000-word budget) **before** `adjust()` (which
removes superseded works and re-sorts). So superseded sections can use up budget and then be
removed, which pushes out valid lower-ranked sections, and the budget drops by
pre-adjustment rank.
- A. Apply superseded/inactive exclusion and score adjustment per passage *before* `expand()`.
  **(Recommended)**
- B. Keep the design's order.

Current code: both functions exist; the order will be set in the `/search` handler, which isn't
written yet.

### Q4. Sections in documents with no headings
The design's prose says a section is "a heading-bounded section or a run of 3–5 paragraphs",
but its code closes a heading-less section only after **1500 words**. Those are very different
excerpt sizes (~300 words vs. up to 1500).
- A. Follow the code: a 1500-word cap. Fewer, larger excerpts; with the 9,000-word budget,
  only about 6 sections fit.
- B. Follow the prose: close after 5 paragraphs or ~500 words, whichever comes first. More
  focused excerpts. **(Recommended, to tune on the golden set)**

Current code: A (`MAX_SECTION_WORDS = 1500`).

### Q5. Heading detection for the actual corpus
`HEADING = (chapter|part|book|section)\s+\S+` comes from the Moby-Dick example. WWC practice
guides use "Recommendation 1", "Step 2", "Appendix A", and numbered headings ("1.2 ...");
MS CCRS uses standard codes ("RF.K.3").
- A. Extend the regex to the patterns Addison sees in the core sources. **(Recommended;
  needs 3–5 real source files)**
- B. Rely on DOCX heading styles and the Q1 PDF heuristics only; drop the regex.

## Smaller (code exists; confirm or change)

### Q6. `section_path` content
The data model has `section_path` (e.g. "Ch. 42"), but the chunker only knows the single
heading that opened the section. Current code: `section_path` is that heading text, or null.
Nested paths (e.g. "Recommendation 2 › Step 1") need heading levels, which DOCX has but txt/PDF
don't. OK to keep single-level for the PoC?

### Q7. Page in citations for multi-page sections
The section's `page` is the page where it **starts**, so a section spanning pp. 12–13 is cited
as "p. 12". Should the label show a range ("pp. 12–13")?

### Q8. Evidence-strength capping details
- Current code lowers only `strong` → `limited`. If the model says `mixed`/`contested` but
  cites only one source, the flag passes through unchanged. Should that fail validation
  (retry), since "the answer must cite both sides"?
- `wwc_evidence_level` has to be set on sections at ingest. How: manually in `meta.json` (a
  list of section headings → level), or by detecting the "Level of evidence: Minimal" text
  WWC guides print under each recommendation?

### Q9. Recency curve is flat after ~4 years
With `half_life=10, floor=0.75`, any work more than ~4.2 years old gets exactly the floor,
so recency only distinguishes works from 2022 onward. That may be intended ("age breaks
ties, doesn't bury"), but every pre-2022 source is weighted the same. Keep it, or lengthen the
half-life (e.g. 20 years reaches the floor at ~8.3 years)?

### Q10. Design-doc maintenance
CLAUDE.md says "if code and the design doc disagree, ask before changing either." When a Q
above is answered, should I update `docs/design.md` to match (keeping a changelog line), or
keep design.md frozen and record outcomes only in `docs/decisions.md`?

## Needs you or AWS (not decisions, but blocking)

- **AWS profile `read-poc`**: no AWS config exists on this machine. Run
  `aws configure sso --profile read-poc` (or `aws configure --profile read-poc`) yourself;
  don't paste credentials into the chat.
- **Bedrock model access** in us-east-1: Titan Text Embeddings V2, and the answer model (which
  one? it goes in `ANSWER_MODEL_ID`). Optionally a rerank model (see Q2).
- **License review + `meta.json`** for the core sources: which files, and where are they? I can
  draft sidecars with the verification fields left empty.
- **Hybrid search on OpenSearch Serverless**: the design's week-2 item "confirm hybrid search
  works on it" needs the collection created (billable, ~$175+/mo idle; I'll ask before creating).
