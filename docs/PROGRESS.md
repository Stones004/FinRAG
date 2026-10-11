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

## Open decisions
- Exchange terms of use for automated download (blocks enabling M02 network adapters and M10 polling). Access method is now known (finding 1); a manual browser download worked on 2026-10-11.
- Local LLM model choice after checking hardware (M09).
