# M02 — Ingestion and Raw Store

**Package:** `src/finrag/ingestion/` · **Depends on:** M01 · **Tests:** `tests/m02_ingestion/`

## Purpose
Get filings (XBRL results, results PDFs, annual-report PDFs, earnings call transcript PDFs) for watch-list companies into an immutable raw store, and register each one exactly once. Sources are pluggable adapters so a blocked or changed exchange endpoint affects one file only.

## Key decisions
- **Start with the local inbox adapter.** The user drops files into `data/inbox/`. This works with no network access and unblocks M03–M09.
- **Exchange adapters (NSE, BSE) are built but disabled** (`settings.sources.enabled`) until access method and terms of use are confirmed. Respect `requests_per_minute`, retries and backoff from config.
- **Backfill** = fetch `settings.history.years_back` years for a newly added ISIN.
- **Content wins over metadata, but nothing is thrown away.** When a file's content conflicts with its filename or listing (ISIN fails the check digit, period or period type differs, basis differs), the raw file is kept and the filing is registered with status `quarantined`. M03 and later modules skip quarantined filings. A `filing_conflict` review item lists each conflict. Reason: the originals are evidence; how often a company files corrected results is itself a signal for the later shenanigans work.
- **`config/sections.yaml` starts here, minimal.** M02 creates it holding only `transcript_markers.qa_start` (copied exactly from the M08 spec), with a header comment saying M03 and M08 extend it. M02 reads nothing else from it.

## Files

### `ingestion/models.py`
- **Does:** `FilingCandidate(identifier, isin, source, filing_type, submission, revision_no, basis, period_start, period_end, period_type, filed_on, source_url, format, local_path | download_url, period_unverified)`; `Rejected(file_name, reason)`; `IngestReport(found, new, duplicate, failed, quarantined, link_only, errors)`. `identifier` is what the source called the company (ISIN or NSE symbol from a filename); `isin` is set once known. `period_unverified` is true when the period was guessed (NSE transcripts) and must be confirmed from content.

### `ingestion/adapters/base.py`
- **Does:** abstract `SourceAdapter` with `name`, `begin_run()`, `list_filings(isin, since) -> list[FilingCandidate]`, `fetch(candidate) -> bytes`, `rejected() -> list[Rejected]`. Adapters never write to DB or disk (they may read the entity table through an injected lookup).

### `ingestion/adapters/local_inbox.py`
- **Input:** files in `settings.paths.inbox` (not `_processed/`).
- **Process:** derive metadata from a filename convention `<ISIN or NSE symbol>__<filing_type>__<period_label>__<basis>[__rev<N>].<ext>` (e.g. `INE524A01029__financial_results__Q4FY26__consolidated__rev1.xml`). Supported `filing_type` values: `financial_results`, `annual_report`, `earnings_call_transcript`. For `earnings_call_transcript` (PDF only) the `period_label` is the **quarter discussed** (e.g. `Q1FY27`), not the call date: `<ISIN>__earnings_call_transcript__Q1FY27__consolidated.pdf`; `filed_on` is not derived from the filename and stays NULL for PDFs. `__rev<N>` marks a revised filing; it is registered as a separate filing with `submission = revision`, `revision_no = N`. For XBRL, **content wins over filename**: read ISIN, period, basis and period type from the file (`xbrl_meta`); any disagreement goes through the quarantine rule above. If the name doesn't match and the file is XBRL, take ISIN/period/basis from the instance itself via `ingestion/xbrl_meta.py` (M03 imports it). Anything else that cannot be turned into a candidate → `Rejected`, and the runner queues it as `inbox_unrecognised`.
- **Output:** candidates; processed inbox files (registered, duplicate, quarantined, link-only) are moved to `inbox/_processed/` (the raw store holds the copy). Rejected files stay in the inbox.

### `ingestion/xbrl_meta.py`
- **Input:** XBRL instance bytes.
- **Process:** read entity identifier, reporting period, and basis/nature-of-report fields. The metadata elements sit in the first context (`OneD`, the filing's own period); the first occurrence of each element is used.
- **Output:** `XbrlMeta(isin, symbol, scrip_code, name, basis, period_start, period_end, period_type_raw, reporting_quarter, audited, level_of_rounding, board_meeting_date, taxonomy_family, raw_fields)`.
- **Elements (from the 2026-10-11 spike, PROGRESS finding 5):** `in-capmkt:ISIN`, `Symbol`, `ScripCode`, `NameOfTheCompany`, `NatureOfReportStandaloneConsolidated`, `DateOfStartOfReportingPeriod`, `DateOfEndOfReportingPeriod`, `TypeOfReportingPeriod`, `ReportingQuarter`, `WhetherResultsAreAuditedOrUnaudited`, `LevelOfRounding`, `DateOfBoardMeetingWhenFinancialResultsWereApproved`; taxonomy family from the `in-capmkt-ent` namespace (`IntegratedFinance_IndAS` / `IntegratedFinance_Banking`). Missing → `None`.
- **`filed_on` for XBRL** = `DateOfBoardMeetingWhenFinancialResultsWereApproved` unless the source supplied a dissemination time. An original and its revision share this date, so M05 orders by `revision_no` next. **Revision filing time (M05 review):** a revision whose board-meeting date equals its original's `filed_on` (a plain date) takes `filed_on` from the exchange filename timestamp `_DDMMYYYYhhmmss_` (`ingestion/filename_time.py`; a convention name may carry it as `__rev1_DDMMYYYYhhmmss_`). **`finrag ingest set-filed-on <filing_id> <YYYY-MM-DD|ISO datetime> --reason "..."`** corrects a stored date; the old value, new value and reason are written to a `jobs` row (`set_filed_on`) and the log, and a reason is required.
- **Validate ISIN** with the standard check digit; `INEONT901020` (letter O) must fail.
- **`TypeOfReportingPeriod` values** seen so far: `Quarterly`, `Half Yearly`. Any other value is a `period_type_unrecognised` conflict: the file is quarantined for review, never a crash (TODO: add values when seen). Period dates are always checked too.

### `ingestion/conflicts.py`
- **Input:** a `FilingCandidate` (what the filename or listing claims) and the file's `XbrlMeta` (and, for transcripts, `TranscriptCheck`).
- **Process:** list every conflict: content ISIN fails the check digit; content ISIN differs from the claimed ISIN; period start or end differs; reporting-period type differs (`Half Yearly` vs `Q4`); basis differs; for a transcript, the quarter named in the title block differs from the claimed quarter (`quarter`), or no recognisable `Qn FYyy` title exists at all (`quarter_unconfirmed`, whether the period came from a filename or a date-based guess); a file declaring an unknown `TypeOfReportingPeriod` value (`period_type_unrecognised`).
- **Output:** `list[Conflict(field, claimed, found, message)]`; empty means consistent.

### `ingestion/adapters/nse.py`, `ingestion/adapters/bse.py`
- **Process:** list and download filings for an ISIN using endpoints from config (`sources.nse.endpoints`). Rate-limited, retrying, timeouts from config. Parsing is pure functions tested on recorded responses in `tests/fixtures/nse/`.
- **NSE endpoints and fields (recorded 2026-10-12):**
  - Integrated filings (`type_Sub` Original|Revision): `qe_Date`, `consolidated` (Standalone|Consolidated), `xbrl`, `pdf_attach` (a URL ending `/null` means none), `broadcast_Date` / `revised_Date`. **`filed_on`** = `broadcast_Date`, or `revised_Date` for revisions (revisions have `broadcast_Date: null`); fall back to `creation_Date`. Used for periods from `sources.nse.integrated_from_period_end`.
  - Old results API (earlier periods): `fromDate`, `toDate`, `consolidated` (Consolidated|Non-Consolidated), `xbrl`, `broadCastDate`. Its `isin` is not trusted (PROGRESS finding 2); the requested ISIN is used.
  - Annual reports: `fromYr`, `toYr`, `fileName`, `broadcast_dttm`, `submission_type` (`New` = original). No ISIN or symbol in the record.
  - Transcripts via corporate announcements: keep records whose `desc` equals `sources.nse.transcript_desc` **and** whose `attchmntText` contains `sources.nse.transcript_text_marker` (case-insensitive); file in `attchmntFile`, date in `an_dt`. Announcements about meeting schedules from the same category are excluded. The announcement does not name the quarter: it is guessed as the last fiscal quarter ended before `an_dt`, flagged `period_unverified`, and confirmed from the PDF title block (mismatch or no recognisable title → quarantine).
- The adapter stays **disabled** until the user confirms terms of use; the session cookie warm-up in `http.py` is unverified against the live site.
- BSE: endpoints still unknown — skeleton only.

### `ingestion/http.py`
- **Does:** `RateLimiter(requests_per_minute, clock, sleep)`; `HttpClient` protocol (`get_json`, `get_bytes`); `HttpxClient` implementing it with timeout, retries, backoff and user agent from config. Tests use a fake clock and `httpx.MockTransport` (no network).

### `ingestion/transcript_check.py`
- **Input:** transcript PDF bytes (page text from `pymupdf` — no OCR here).
- **Process:** some companies (e.g. Gabriel India) file only a short cover letter that links to a transcript on their own website. Detect this: page count ≤ `ingestion.transcript_link_only_max_pages` **and** no Q&A text (`transcript_markers.qa_start` from `sections.yaml` not found). Such a file is **not** registered for indexing: it goes to `review_queue` with `item_type = transcript_link_only` and a payload holding the file name, the page count, the link text/URLs found in the letter, and the quarter named in the subject (flagged as unverified). The raw file is still kept in the raw store. Also reads the quarter named in the "Earnings Conference Call" title line (`title_quarter`) for the conflict check.
- **Output:** `TranscriptCheck(is_link_only, links, subject_quarter, title_quarter, page_count)`.
- **Real examples:** HDFC Bank and Netweb file the full transcript attached to a 1–2 page cover letter (HDFC's letter also links to its website) — these have Q&A text and are not link-only. The page-count rule alone is not enough, which is why the Q&A-text check is required.

### `ingestion/raw_store.py`
- **Input:** bytes + candidate.
- **Process:** compute sha256; write to `raw/<ISIN>/<fiscal_year>/<filing_id>.<ext>`; never overwrite; set file read-only. `<fiscal_year>` is the fiscal-year label of `period_end` (annual reports: their own FY).
- **Output:** `(filing_id, raw_path, sha256)`.

### `ingestion/registry.py`
- **Process:** insert into `filings` if sha256 is new (status `fetched`, or `quarantined` with conflicts); if sha256 exists, return existing filing_id (duplicate; a byte-identical revision is a duplicate too). Status transitions: `fetched → parsed → normalized` or `failed` with error text. Quarantine: write a `filing_conflict` review item (`filing_id`, `file_name`, `conflicts`, and `superseded_by` listing a revision filing for the same ISIN, period and basis, e.g. "superseded by FIL-…"; kept up to date when the revision arrives later). `release(filing_id)` sets the filing to `fetched` and resolves the item; `ignore(filing_id)` keeps it quarantined and marks the item `ignored`. `revision_no` is the `__rev<N>` number, or 1 + the highest existing for the same ISIN, type, period and basis when the source gives none. Also `DbEntityLookup` (identifier → ISIN, ISIN → symbol) and the review-queue writers for `inbox_unrecognised` and `transcript_link_only`.
- **Output:** `register(candidate, raw, conflicts=()) -> (filing_id, is_new)`; `pending(status) -> list[filing]`.

### `ingestion/runner.py`
- **Input:** ISIN list (default: active watch list), `mode = incremental | backfill`.
- **Process:** for each enabled adapter → list → skip known (by URL or hash) → fetch → check → store → register; one `jobs` row per ISIN; failures recorded, never stop the whole run. Rejected inbox files are queued once per run.
- **Output:** `IngestReport` per ISIN.

### `ingestion/cli.py`
- **Commands:** `finrag ingest run [--isin X] [--backfill]`, `finrag ingest inbox`, `finrag ingest list --isin X`, `finrag ingest release <filing_id>`, `finrag ingest ignore <filing_id>`.

### Settings added by this module
`paths.inbox_processed`; `ingestion.transcript_link_only_max_pages`; `sources.nse.endpoints`, `integrated_filing_type`, `transcript_desc`, `transcript_text_marker`, `integrated_from_period_end`, `legacy_result_periods`, `announcement_window_days`.

## Tests and acceptance criteria
1. Dropping a correctly named file into the inbox → one `filings` row, file in raw store, read-only.
2. Same file dropped again (any name) → no new row (sha256 dedupe).
3. Badly named non-XBRL file → review-queue item, no filing row.
4. Raw files are never modified: a test rehashes after a full run.
5. A failing adapter for one ISIN doesn't stop others; the failure is in `jobs`.
6. Rate limiter enforces `requests_per_minute` (tested with a fake clock).
7. With `sources.enabled: [local_inbox]`, no network adapter is called.
8. A correctly named `earnings_call_transcript` PDF (period = quarter discussed) → one `filings` row with `filing_type = earnings_call_transcript`, `format = pdf`, period set to that quarter.
9. A transcript PDF with ≤ 2 pages and no Q&A text → `review_queue` item `transcript_link_only` carrying the link text, and no `filings` row for indexing; a full transcript with a 1–2 page cover letter attached is not flagged.
10. An XBRL file whose content conflicts with its filename (period type Half Yearly vs Q4, ISIN check digit) → raw file stored, filing registered `quarantined`, a `filing_conflict` item listing each conflict and naming the superseding revision when one exists; `pending(fetched)` does not return it; `release` makes it `fetched`.
11. NSE parsing against the recorded responses: integrated, old-format, annual-report and announcement records become the right candidates (`filed_on` from `broadcast_Date`, `revised_Date` for revisions, `broadcast_dttm`, `an_dt`); the "Schedule of meet" announcement is excluded.

## Real-data spike (do once, with the user)
When the user provides 2–3 real filings per type, record in `PROGRESS.md`: file formats, which statements appear in quarterly vs annual filings, whether standalone/consolidated are separate files, how periods and basis are encoded.

## Out of scope
Parsing contents beyond the minimal XBRL metadata helper.
