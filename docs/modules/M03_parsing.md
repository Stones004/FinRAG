# M03 — Parsing (XBRL and PDF)

**Package:** `src/finrag/parsing/` · **Depends on:** M02 · **Tests:** `tests/m03_parsing/`

## Purpose
Turn raw filings into `parsed_facts` (numbers as reported, with location) and page-level text for the narrative module. No mapping to canonical concepts here — that is M04.

## Files

Uses `ingestion/xbrl_meta.py` (M02) for filing-level metadata; extend that file here if real filings need more fields.

### Support files (added during the build)
- `parsing/models.py` — `ParsedFact`, `TextFact`, `Dimension`, `dimensions_json()`.
- `parsing/numbers.py` — `parse_number` (`(1,234)` → -1234, Indian grouping, dash/NIL → None; never repairs letters for digits).
- `parsing/sections.py` — validates and compiles the M03 keys of `config/sections.yaml`.
- `parsing/text_quality.py` — the text-quality score and its vocabulary.
- `parsing/table_layout.py` — positioned words → lines → numeric columns → rows (shared by native and OCR pages).

### `parsing/xbrl_parser.py`
- **Input:** filing row + raw XBRL file.
- **Process:** parse with `lxml`; read `context` elements (period: instant or duration; dimensions/segments) and `unit` elements; for each numeric fact: concept QName, contextRef, unitRef, decimals, value; non-numeric facts kept separately (text blocks are useful for M08).
- **Output:** `list[ParsedFact]` with `extractor='xbrl'`, `confidence=1.0`, `source_loc = contextRef + concept`; `scale = 0` (XBRL values are full units; `decimals` records rounding).
- **Rules:** keep the full QName (prefix + local name) in `concept_raw`; never drop a fact. Dimension members go in the **`dimensions_json`** column (`NULL` = entity total; otherwise a JSON list of `{axis, member}`, plus `typed: true` for typed members) and are repeated in `source_loc` for readability (`<contextRef> <concept> [axis=member; ...]`). `decimals="INF"` is stored as `NULL`. `unit_raw` is the measure as written (`iso4217:INR`, `iso4217:INR/xbrli:shares`, `xbrli:pure`). A nil numeric fact is kept with `value = NULL`. A fact whose number cannot be read fails the filing (never skipped).
- **Non-numeric facts** (metadata, text blocks) are not `parsed_facts` rows: they go to `processed/<filing_id>/text_facts.jsonl` (`{concept, context, period_start, period_end, dimensions, value}`) for M08.

### `parsing/pdf_text.py`
- **Input:** raw PDF.
- **Text quality (`parsing/text_quality.py`):** score = share of alphabetic tokens of length ≥ `parsing.pdf.quality_min_token_len` (3) found in `config/vocab/common_en.txt` (`paths.vocab_common_en`) or in `financial_terms` in `sections.yaml`; a token also counts if it is a listed word plus a regular ending (`s es d ed ing ly`). No countable tokens → 0.0. The threshold `parsing.pdf.min_text_quality` was calibrated on real pages (see PROGRESS "M03 notes").
- **Process:** `pymupdf` page text; if a page has fewer than `parsing.pdf.min_text_chars_for_native` characters, **or** its text-quality score is below `parsing.pdf.min_text_quality`, OCR it via the configured `OcrBackend` (see below), if `parsing.ocr.ocr_enabled`. Real case: Netweb's Q4 FY26 results PDF is a scan whose embedded OCR layer reads "prolit", "OPERATINC". OCR output from **either backend** passes the same text-quality check (`min_text_quality`); a page that still fails is kept with a low-quality flag and listed in the job stats, never silently accepted. The better-scoring text (native or OCR) is kept. A completely blank page is counted (`blank_pages`), not flagged. If the backend cannot run (`OcrUnavailable`) the page is flagged with `ocr_error` and the backend is not retried for the rest of that file.
- **Output:** `processed/<filing_id>/pages.jsonl` — one line per page: `{page, text, method: native|ocr, ocr_backend: tesseract|chandra|null, char_count, lines, quality, low_quality}` plus, when they apply, `ocr_reason` (`too_short|low_quality`), `ocr_error`, `native_quality` (the native score when OCR replaced it) and `words` (`[text, x0, x1, top, bottom]` in points, OCR pages only). For `earnings_call_transcript` PDFs with a native text layer, `lines` is `[{text, bold, font, size}]` from `pymupdf` span flags/fonts (used by M08's speaker-label fallback; absent or empty for OCR pages and other filing types).

### `parsing/ocr/base.py`, `parsing/ocr/tesseract.py`, `parsing/ocr/chandra.py`, `parsing/ocr/factory.py`
- **Interface:** `OcrBackend.ocr_page(image: bytes) -> OcrResult(text, tables, backend, words)` (`words` = positioned words in points, filled by Tesseract; `tables` = cell matrices a backend finds itself, Chandra once its parser exists); `name` property; `factory.get_backend()` picks the implementation from `parsing.ocr.backend`.
- **Tesseract:** `pytesseract` (`image_to_data`) with `parsing.ocr.ocr_lang`; page rendered at `parsing.ocr.ocr_dpi`; `parsing.ocr.tesseract_cmd` (blank = use PATH) names a non-PATH install. Ruled table lines at least `parsing.ocr.rule_min_length_pt` long are erased from the image first (Tesseract otherwise skips the text inside ruled header boxes, e.g. "Quarter ended"). `parsing.ocr.tesseract_passes` lists one option string per pass (default `--psm 3` then `--psm 11`): the first pass gives the page text and words, later passes only add words that overlap none already found (layout analysis and sparse-text mode each miss cells the other finds).
- **Chandra:** render the page to an image at `parsing.ocr.ocr_dpi`, downscale so the longest side ≤ `parsing.ocr.chandra.max_image_px`, send it to the OpenAI-compatible vision endpoint (`parsing.ocr.chandra.base_url`, `model`, `timeout_s`; `POST /chat/completions` with the image as a data-URL content part), and parse the returned markdown/HTML into text and tables. **Status: interface, factory, downscale, request and error handling are built; the prompt and the response parser are TODO** (a sample model response is needed first; until then `ocr_page` raises `OcrUnavailable("TODO ...")` after the request, and acceptance 5b's table-parsing half is an `xfail`). Model unavailable or `model: ""` → `OcrUnavailable`; the page is flagged, not skipped silently.
- **Rules:** OCR'd numbers are never trusted over XBRL. Facts from OCR carry `extractor = 'ocr'` and `ocr_backend = <name>`. Only one GPU model is loaded at a time: OCR runs during ingestion, not alongside the query-time LLM.

### `parsing/page_classifier.py`
- **Input:** pages, `config/sections.yaml` → `statement_pages` patterns.
- **Process:** score each page by the number of distinct `statement_pages` patterns that match (regexes, case-insensitive); the highest type wins if it reaches `statement_page_min_score`, a tie is left unlabelled. Basis = the one basis keyword family found in the first `parsing.pdf.basis_scan_lines` non-empty lines (both or none → `unknown`). A statement page with no unit line of its own takes the unit of the latest earlier statement page that has one (only within the same filing, only from the nearest earlier statement page that has its own unit line, never chained, never if the page has its own unit line; `unit_from_page` records it, the facts' `source_loc` gets a `;unit_page=<n>` suffix and their confidence drops by `parsing.pdf.unit_inherited_confidence_penalty`; Netweb states "in millions" once, above the P&L). Unit = the earliest `unit_patterns` match on the page (the rupee sign often extracts as a backtick, so a currency mark may sit between "in" and the unit).
- **Output:** `list[PageLabel(page, statement_type | None, basis, unit_hint, score)]`. Pages below `statement_page_min_score` are unlabeled.

### `parsing/pdf_tables.py`
- **Input:** labeled statement pages.
- **Process:** positioned words (pdfplumber `extract_words` for native pages, the backend's `words` for OCR pages) → lines (`row_tolerance_pt`) → numeric columns (right edges within `column_tolerance_pt`, on ≥ `min_table_rows` lines and ≥ `column_min_support_ratio` of the best column) → rows (label + cells; wrapped labels joined; note references and page-number footers dropped). Column headers are read from up to `header_max_lines` lines above the first data row: a date per column and a period kind ("Quarter ended", "Year ended", ...) from the nearest kind phrase; balance-sheet columns are instants. Numbers are cleaned with `parse_number`; the page's unit key is `unit_raw` and its multiplier from `settings.units.multipliers` is `scale` (no unit line → both NULL). Confidence = mean of header found (over columns that have a date), column-count consistency and numeric ratio. A number whose decimal places differ from the table's usual count (OCR often drops the point: `948.22` read as `94822`) gets confidence capped at `parsing.pdf.format_outlier_confidence` and is listed under `format_outliers` in the review item, even when the table as a whole is fine. **Deviation:** pdfplumber's line-based table finder is not used; one geometry builder serves native and OCR pages. A rotated native page is not read (OCR it).
- **Output:** `list[ParsedFact]` with `extractor='pdf_table'|'ocr'` (+ `ocr_backend` when `ocr`), `label_raw`, `concept_raw = None`, `source_loc = page:row:col` (1-based; row = line number on the page), `confidence`, and **`statement_type`** (the page's `PageLabel.statement_type`; the `parsed_facts.statement_type` column, `NULL` for XBRL; schema v3, migrated in place like `dimensions_json`) so that M04 can accept a label only for its own statement. Only columns with a period date give facts. Tables below `min_table_confidence`, and labelled pages with no readable table or no period header, → `review_queue` (`low_confidence_table`, one open item per filing and page); facts of low-confidence tables are still stored with their confidence.

### `parsing/runner.py`
- **Input:** filings with status `fetched`.
- **Process:** route by format: XBRL → `xbrl_parser` (+ text blocks); PDF → `pdf_text`, then `page_classifier` + `pdf_tables` for results PDFs (annual reports and transcripts: page text only, every page kept; M08 applies the section whitelist). Write `parsed_facts` (a filing's rows are replaced on re-parse); set status `parsed` or `failed` (with the error). Quarantined filings are never parsed; a `normalized` filing is not re-parsed. The raw file's sha256 is checked before parsing.
- **Output:** one `jobs` row per filing with counts (facts, pages, OCR pages, low-quality pages, unread statement pages, review items).

### `parsing/cli.py`
- `finrag parse run [--filing ID]`, `finrag parse show --filing ID` (print facts, first N).

### `config/sections.yaml` (the statement part is created here; M08 adds narrative sections)
The file as built also has `table_header` (date and period-kind patterns), `table_labels` (words that continue a wrapped label) and `financial_terms` (extra known words for the quality score); the unit patterns allow a currency mark between "in" and the unit. Starting version from the original spec:
```yaml
statement_pages:
  profit_and_loss: ["statement of profit and loss", "financial results", "total income", "profit before tax"]
  balance_sheet:  ["balance sheet", "statement of assets and liabilities", "total equity and liabilities"]
  cash_flow:      ["cash flow", "cash generated from operations", "net cash"]
basis_patterns:
  consolidated: ["consolidated"]
  standalone:   ["standalone"]
unit_patterns:   # regex → unit key in settings.units.multipliers
  crore: ["in\\s*crores?", "₹\\s*crore", "rs\\.?\\s*in\\s*crores?"]
  lakh:  ["in\\s*lakhs?", "₹\\s*lakh"]
  million: ["in\\s*millions?"]
```
These are starting patterns; extend from real filings.

### New settings (`parsing.pdf`, `parsing.ocr`, `paths`)
`paths.vocab_common_en`, `parsing.ocr.tesseract_cmd`, `parsing.pdf.quality_min_token_len`, `row_tolerance_pt`, `column_tolerance_pt`, `min_table_rows`, `column_min_support_ratio`, `header_max_lines`, `basis_scan_lines`; `min_text_quality` recalibrated to 0.70, `format_outlier_confidence`, `parsing.ocr.rule_min_length_pt`, `parsing.ocr.tesseract_passes`.

## Tests and acceptance criteria
1. Synthetic XBRL fixture: every numeric fact parsed with correct period, unit and decimals; count matches the fixture.
2. `(1,234)` → `-1234`; `1,23,456` (Indian grouping) → `123456`; `—` → null.
3. Synthetic results PDF: P&L page classified; table values match the fixture within exact equality.
4. Unit line "₹ in crore" → scale 10,000,000.
5. A page with no text → OCR path chosen (both backends mocked in unit tests; no network).
5a. Backend selection follows `parsing.ocr.backend`; the same text-quality check is applied to tesseract and chandra output; a poor-quality OCR result is flagged.
5b. Chandra backend (mocked HTTP): image is downscaled to ≤ `max_image_px`; a returned markdown/HTML table is parsed into cells; `OcrUnavailable` handled.
5c. Facts from OCR have `extractor = 'ocr'` and `ocr_backend` set; XBRL values are never overridden by OCR values.
6. Low-confidence table → review-queue item; facts kept with confidence.
5b'. (open) A returned markdown/HTML table is parsed into cells — waits for the Chandra response parser (xfail test).
7. Golden (if present): totals parsed from a real XBRL file match `golden_values.yaml` (entries with `verified: true` only).
8. Golden, non-circular (skipped without Tesseract; passes with it): Netweb Q4 FY26 XBRL revision and the scanned results PDF (real OCR) agree on revenue, profit, total assets and operating cash flow within rounding.

## Out of scope
Concept mapping, unit conversion to rupees, deduplication across filings (M04/M05).
