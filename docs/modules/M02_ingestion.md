# M02 — Ingestion and Raw Store

**Package:** `src/finrag/ingestion/` · **Depends on:** M01 · **Tests:** `tests/m02_ingestion/`

## Purpose
Get filings (XBRL results, results PDFs, annual-report PDFs) for watch-list companies into an immutable raw store, and register each one exactly once. Sources are pluggable adapters so a blocked or changed exchange endpoint affects one file only.

## Key decisions
- **Start with the local inbox adapter.** The user drops files into `data/inbox/`. This works with no network access and unblocks M03–M09.
- **Exchange adapters (NSE, BSE) are built but disabled** (`settings.sources.enabled`) until access method and terms of use are confirmed. Respect `requests_per_minute`, retries and backoff from config.
- **Backfill** = fetch `settings.history.years_back` years for a newly added ISIN.

## Files

### `ingestion/models.py`
- **Does:** `FilingCandidate(isin, source, filing_type, basis, period_start, period_end, period_type, filed_on, source_url, format, local_path | download_url)`; `IngestReport(found, new, duplicate, failed)`.

### `ingestion/adapters/base.py`
- **Does:** abstract `SourceAdapter` with `name`, `list_filings(isin, since) -> list[FilingCandidate]`, `fetch(candidate) -> bytes`. Adapters never write to DB or disk.

### `ingestion/adapters/local_inbox.py`
- **Input:** files in `settings.paths.inbox`.
- **Process:** derive metadata from a filename convention `<ISIN or NSE symbol>__<filing_type>__<period_label>__<basis>[__rev<N>].<ext>` (e.g. `INE524A01029__financial_results__Q4FY26__consolidated__rev1.xml`). `__rev<N>` marks a revised filing; it is registered as a separate filing with `submission = revision`. For XBRL, **content wins over filename**: read ISIN, period, basis and period type from the file (`xbrl_meta`); if they disagree with the filename, or the ISIN fails check-digit validation, send to review (see PROGRESS findings 2–3). If the name doesn't match, and the file is XBRL, read ISIN/period/basis from the instance itself via `ingestion/xbrl_meta.py` (below; M03 imports it). Otherwise → `review_queue`.
- **Output:** candidates; processed inbox files are moved to `inbox/_processed/` (the raw store holds the copy).

### `ingestion/xbrl_meta.py`
- **Input:** XBRL instance bytes.
- **Process:** read entity identifier, reporting period, and basis/nature-of-report fields.
- **Output:** `XbrlMeta(isin, period_start, period_end, basis, raw_fields)`.
- **Elements (from the 2026-10-11 spike, PROGRESS finding 5):** `in-capmkt:ISIN`, `Symbol`, `ScripCode`, `NameOfTheCompany`, `NatureOfReportStandaloneConsolidated`, `DateOfStartOfReportingPeriod`, `DateOfEndOfReportingPeriod`, `TypeOfReportingPeriod`, `ReportingQuarter`, `WhetherResultsAreAuditedOrUnaudited`, `LevelOfRounding`; taxonomy family from the namespace and file-name prefix (`INTEGRATED_FILING_INDAS_` / `INTEGRATED_FILING_BANKING_`). Missing → `None` → review.
- **Validate ISIN** with the standard check digit; `INEONT901020` (letter O) must fail.

### `ingestion/adapters/nse.py`, `ingestion/adapters/bse.py`
- **Process:** list and download filings for an ISIN using endpoints from config. Rate-limited, retrying, timeouts from config.
- **NSE endpoints known** (PROGRESS finding 1): old results API (pre-2025) and integrated-filing API (2025+), annual-reports API; files on `nsearchives.nseindia.com`. The API requires NSE session cookies. Implement both list endpoints, prefer integrated filings, and map `type_Sub` to `submission`. Keep the adapter **disabled** until the user confirms terms of use. Test only against recorded fixture responses.
- BSE: endpoints still unknown — skeleton only.

### `ingestion/raw_store.py`
- **Input:** bytes + candidate.
- **Process:** compute sha256; write to `raw/<ISIN>/<fiscal_year>/<filing_id>.<ext>`; never overwrite; set file read-only.
- **Output:** `(filing_id, raw_path, sha256)`.

### `ingestion/registry.py`
- **Process:** insert into `filings` if sha256 is new (status `fetched`); if sha256 exists, return existing filing_id (duplicate). Status transitions: `fetched → parsed → normalized` or `failed` with error text.
- **Output:** `register(candidate, raw) -> (filing_id, is_new)`; `pending(status) -> list[filing]`.

### `ingestion/runner.py`
- **Input:** ISIN list (default: active watch list), `mode = incremental | backfill`.
- **Process:** for each enabled adapter → list → skip known (by URL or hash) → fetch → store → register; one `jobs` row per ISIN; failures recorded, never stop the whole run.
- **Output:** `IngestReport` per ISIN.

### `ingestion/cli.py`
- **Commands:** `finrag ingest run [--isin X] [--backfill]`, `finrag ingest inbox`, `finrag ingest list --isin X`.

## Tests and acceptance criteria
1. Dropping a correctly named file into the inbox → one `filings` row, file in raw store, read-only.
2. Same file dropped again (any name) → no new row (sha256 dedupe).
3. Badly named non-XBRL file → review-queue item, no filing row.
4. Raw files are never modified: a test rehashes after a full run.
5. A failing adapter for one ISIN doesn't stop others; the failure is in `jobs`.
6. Rate limiter enforces `requests_per_minute` (tested with a fake clock).
7. With `sources.enabled: [local_inbox]`, no network adapter is called.

## Real-data spike (do once, with the user)
When the user provides 2–3 real filings per type, record in `PROGRESS.md`: file formats, which statements appear in quarterly vs annual filings, whether standalone/consolidated are separate files, how periods and basis are encoded.

## Out of scope
Parsing contents beyond the minimal XBRL metadata helper.
