# FinRAG — Build Progress

Update this file at the end of every module. Newest entry at the bottom.

| Module | Status | Date | Notes / deviations from spec |
|---|---|---|---|
| M00 Foundation | not started | | |
| M01 Watch list | not started | | |
| M02 Ingestion | not started | | |
| M03 Parsing | not started | | |
| M04 Normalization | not started | | |
| M05 Facts | not started | | |
| M06 Validation | not started | | |
| M07 Metrics | not started | | |
| M08 Narrative | not started | | |
| M09 Query + LLM | not started | | |
| M10 Scheduler | not started | | |
| M11 UI | not started | | |

## Findings from real data
Record anything learned from real filings that affects the design (formats, missing
statements, concept names, quirks). Each finding: date, company ISIN, filing ID, what was found,
which spec was updated.

### 2026-10-11 — data spike (Gabriel, HDFC Bank, Netweb; 17 XBRL + 4 PDFs from NSE)
Files: `data/inbox/`, provenance in `data/reference/inbox_manifest_2026-10-11.csv`.

1. **Two NSE result sources.** `/api/corporates-financial-results` (old format) only has results up to ~Dec 2024.
   From the FY25 Q4 results onward, results are under **Integrated Filing – Financials**:
   `/api/integrated-filing-results?index=equities&symbol=X&type=Integrated%20Filing-%20Financials&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY`
   (fields: `qe_Date, consolidated, audited, type_Sub (Original|Revision), broadcast_Date, xbrl, ixbrl, pdf_attach`).
   Annual reports: `/api/annual-reports?index=equities&symbol=X` (field `fileName`). Files live on `nsearchives.nseindia.com`.
   The API needs NSE browser cookies (works from a page on nseindia.com; plain HTTP clients are typically blocked). → M02.
2. **API metadata can be wrong.** The old API lists HDFC Bank as `INE040A01018` (pre-split ISIN); the XBRL and NSE equity list say `INE040A01034`. → ISIN comes from file content or the securities master, never the listing API. Old ISINs go into `entity_aliases`.
3. **Filer errors are real and get revised.** Netweb's FY25 and FY26 Q4 originals are tagged `TypeOfReportingPeriod = Half Yearly` with period Oct–Mar (H2); the FY26 original also has ISIN typo `INEONT901020` (letter O). Revisions (`type_Sub = Revision`) correct these. Gabriel FY26 Q4 consolidated also has a revision. → Revisions are separate filings; filenames use `__rev<N>`; ISIN typo should be caught by ISIN check-digit validation and sent to review.
4. **Taxonomy:** namespace `in-capmkt` (`http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt`); file comment gives version (e.g. `IFIndAs V2.1`). Two families: `INTEGRATED_FILING_INDAS_*` (companies) and `INTEGRATED_FILING_BANKING_*` (banks). Banks use different concepts: `InterestEarned`, `InterestExpended`, `OperatingExpenses`, `ProvisionsOtherThanTaxAndContingencies`, `ProfitLossFromOrdinaryActivitiesBeforeTax`, `ProfitLossForThePeriod`, `Deposits`, `Advances`, `Borrowings`, `CapitalAndLiabilities`. Companies use `RevenueFromOperations`, `ProfitBeforeTax`, `ProfitLossForPeriod`, `Assets`, `EquityAndLiabilities`, `Equity`, `CashFlowsFromUsedInOperatingActivities`, etc. → M04 taxonomy.
5. **Filing-level metadata elements** (for `ingestion/xbrl_meta.py`): `ISIN`, `Symbol`, `ScripCode`, `NameOfTheCompany`, `NatureOfReportStandaloneConsolidated` (`Standalone|Consolidated`), `DateOfStartOfReportingPeriod`, `DateOfEndOfReportingPeriod`, `TypeOfReportingPeriod`, `ReportingQuarter`, `WhetherResultsAreAuditedOrUnaudited`, `LevelOfRounding` (`Millions|Crores|…`).
6. **Values are full rupees** with a `decimals` attribute (e.g. `-4`, `-6`); `LevelOfRounding` is presentation only — do not multiply. Unit `INR`.
7. **What each filing contains (no dimensions):** Q1/Q3: quarter P&L only (+ few instants). Q2: quarter + H1 P&L, balance sheet at 30 Sep, H1 cash flow. Q4: quarter + FY P&L, balance sheet at 31 Mar, FY cash flow. **No prior-period comparatives** are tagged → restatements are detected from revisions and later filings, not from comparative columns.
8. **Dimensions:** segment data uses `ReportableSegments<N>Member`; other-expense breakdown uses `OtherExpenses<N>Member`. Facts with dimensions must not be mixed with entity totals.
9. **Netweb files standalone only** (no subsidiaries) → `basis: standalone` in watchlist.
10. **PDFs:** annual reports have text layers. Netweb's Q4 results PDF is a scan with a poor embedded OCR layer ("prolit", "OPERATINC") → the PDF parser must score text quality, not just character count, before trusting a text layer. Statement pages: p.6 P&L, p.7 balance sheet, p.8 cash flow; unit "₹ in millions".
11. **Golden values** in `tests/golden/golden_values.yaml` are a DRAFT taken from XBRL (`verified: false`). They must be checked against PDFs before being used to test the XBRL parser.

### 2026-10-11 — transcripts and Gabriel (4 real transcripts: HDFC Bank Q4 FY26/Q1 FY27, Netweb Q4 FY26/Q1 FY27)
12. **Gabriel files transcripts as website links only**, and its cover letter for the Q1 FY27 call names "Q1 FY26" in the subject. (Reported by the user from the NSE announcement; no Gabriel transcript PDF is in `data/inbox/`.) → M02 `transcript_link_only` check; quarter is confirmed from content, not the subject.
13. **Transcript PDF layout** (HDFC, Netweb): 1–2 page exchange cover letter, then the transcript (title block "<Company> Q<n> FY<yy> Earnings Conference Call", cover participant block `MANAGEMENT: MR. NAME – ROLE – COMPANY`, `MODERATOR: MS. NAME – FIRM` at Netweb). HDFC's cover letter also carries a link to its website but the full transcript is annexed. → `transcript_markers` in M08.
14. **Speaker lines are `Name:`** (first-name-only occurs: `Pranav:`, `Omkar:`, `Akshay:`). Analysts are introduced by `... question is from the line of <Name> from <Firm>`; variants: `<Name>, <Firm>`, no firm (`Vatsal Aggarwal from -- who is a Retail Investor`), firm wrapped over two lines. The intro is not always spoken by the Moderator (Netweb's IR adviser reads it). The Netweb host (Seema Nayak, ICICI Securities) is a moderator in prepared remarks and an analyst in the Q4 FY26 Q&A.
15. **Netweb transcripts lose speaker labels in the text layer** (continuing paragraphs unlabeled; labels stacked at page top; one generic `Management:` label). Attribution must tolerate this and report unattributed turns instead of guessing. HDFC labels every turn.
16. Page footers `Page N of M` (Netweb) and repeating header text that can sit on the same line as a label (`Moderator:    HDFC Bank Limited`). Extraction showed the dash in the cover block as an unknown character (U+FFFD) or `--`.

## Open decisions
- 2026-10-11 — Narrative sources: earnings call transcripts become the primary narrative source for quarterly "why" questions; annual reports are limited to whitelisted sections (`narrative.annual_report_sections_include`). Rationale: annual reports run 130–680 pages and are mostly irrelevant to financial questions, but the auditor report, notes, related-party and contingent sections are required for the later shenanigans work, so annual reports are kept (whitelisted, with skipped pages recorded in a per-filing summary), not dropped. Specs updated: MASTER_BUILD §1.1/§7.2/§7.5, M02, M08, M09, settings.yaml.
- 2026-10-11 — Decisions recorded with this spec revision:
  1. Transcripts as a narrative source (`earnings_call_transcript`, PDF, period = quarter discussed): they are where management explains quarterly results, and annual reports cannot answer quarterly "why" questions. NSE listing uses corporate announcements (subject "Analysts/Institutional Investor Meet/Con. Call Updates", attachment text mentions "transcript"); response field names are still to verify from real responses. Link-only cover letters go to `review_queue` as `transcript_link_only`.
  2. Annual reports limited to whitelisted sections (`narrative.annual_report_sections_include`); skipped pages recorded in a per-filing summary (`not_whitelisted` | `unknown_section`).
  3. OCR behind an `OcrBackend` interface, `parsing.ocr.backend: tesseract | chandra` (Chandra via an OpenAI-compatible vision endpoint in LM Studio): scanned results PDFs have unreliable embedded text layers, and a stronger OCR model may read tables better. OCR numbers are never trusted over XBRL. The `ocr_*` keys moved from `parsing.pdf` to `parsing.ocr`. Chandra model name and output layout are unverified.
  4. LLM provider `openai_compatible` (LM Studio default, `http://localhost:1234/v1`): one interface covers LM Studio and Ollama; `llm/ollama.py` replaced by `llm/openai_compat.py`. Model name left blank until a 3–4B instruct GGUF is chosen.
  5. Hardware constraint recorded (RTX 4050 6 GB, 16 GB RAM): one GPU model at a time (OCR at ingestion, LLM at query time); embeddings on CPU (`narrative.embedding_device: cpu`).
- 2026-10-11 — Period handling for transcripts in the planner: H1/9M questions search transcripts for every quarter in the period; annual-report sections only when the period ends at fiscal year-end; FY questions use all four quarterly transcripts plus annual-report sections; citations are tagged with their quarter.
- 2026-10-11 — Transcript closing remarks (after the final hand-over) are their own section `closing_remarks`, not part of `qa`: they often hold guidance useful for outlook questions and answer no question.
- 2026-10-11 — Speaker-label fallback from PDF font info (bold span at line start ending in `:`) added to M08 for Netweb's lost labels. **Unverified:** inspect PyMuPDF spans in the four inbox transcripts to confirm labels are bold before relying on it (M03 must also store per-line span info for transcript PDFs).
- Exchange terms of use for automated download (blocks enabling M02 network adapters and M10 polling). Access method is now known (finding 1); a manual browser download worked on 2026-10-11.
- Local LLM model choice after checking hardware (M09).
