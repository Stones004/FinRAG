# M03 — Parsing (XBRL and PDF)

**Package:** `src/finrag/parsing/` · **Depends on:** M02 · **Tests:** `tests/m03_parsing/`

## Purpose
Turn raw filings into `parsed_facts` (numbers as reported, with location) and page-level text for the narrative module. No mapping to canonical concepts here — that is M04.

## Files

Uses `ingestion/xbrl_meta.py` (M02) for filing-level metadata; extend that file here if real filings need more fields.

### `parsing/xbrl_parser.py`
- **Input:** filing row + raw XBRL file.
- **Process:** parse with `lxml`; read `context` elements (period: instant or duration; dimensions/segments) and `unit` elements; for each numeric fact: concept QName, contextRef, unitRef, decimals, value; non-numeric facts kept separately (text blocks are useful for M08).
- **Output:** `list[ParsedFact]` with `extractor='xbrl'`, `confidence=1.0`, `source_loc = contextRef + concept`; `scale = 0` (XBRL values are full units; `decimals` records rounding).
- **Rules:** keep the full QName (prefix + local name) in `concept_raw`; keep dimension members in `source_loc`; never drop a fact.

### `parsing/pdf_text.py`
- **Input:** raw PDF.
- **Process:** `pymupdf` page text; if a page has fewer than `parsing.pdf.min_text_chars_for_native` characters, **or** its text-quality score (share of tokens that are dictionary words / known financial terms) is below `parsing.pdf.min_text_quality`, OCR it via the configured `OcrBackend` (see below), if `parsing.ocr.ocr_enabled`. Real case: Netweb's Q4 FY26 results PDF is a scan whose embedded OCR layer reads "prolit", "OPERATINC". OCR output from **either backend** passes the same text-quality check (`min_text_quality`); a page that still fails is kept with a low-quality flag and listed in the job stats, never silently accepted.
- **Output:** `processed/<filing_id>/pages.jsonl` — one line per page: `{page, text, method: native|ocr, ocr_backend: tesseract|chandra|null, char_count, lines}`. For `earnings_call_transcript` PDFs with a native text layer, `lines` is `[{text, bold, font, size}]` from `pymupdf` span flags/fonts (used by M08's speaker-label fallback; absent or empty for OCR pages and other filing types).

### `parsing/ocr/base.py`, `parsing/ocr/tesseract.py`, `parsing/ocr/chandra.py`, `parsing/ocr/factory.py`
- **Interface:** `OcrBackend.ocr_page(image: bytes) -> OcrResult(text, tables, backend)`; `name` property; `factory.get_backend()` picks the implementation from `parsing.ocr.backend`.
- **Tesseract:** `pytesseract` with `parsing.ocr.ocr_lang`; page rendered at `parsing.ocr.ocr_dpi`.
- **Chandra:** render the page to an image at `parsing.ocr.ocr_dpi`, downscale so the longest side ≤ `parsing.ocr.chandra.max_image_px`, send it to the OpenAI-compatible vision endpoint (`parsing.ocr.chandra.base_url`, `model`, `timeout_s`; `POST /chat/completions` with the image as a data-URL content part), and parse the returned markdown/HTML into text and tables. The exact prompt and response layout are TODO: confirm against the real model output before building the table parser (do not assume a format). Model unavailable or `model: ""` → `OcrUnavailable`; the page is flagged, not skipped silently.
- **Rules:** OCR'd numbers are never trusted over XBRL. Facts from OCR carry `extractor = 'ocr'` and `ocr_backend = <name>`. Only one GPU model is loaded at a time: OCR runs during ingestion, not alongside the query-time LLM.

### `parsing/page_classifier.py`
- **Input:** pages, `config/sections.yaml` → `statement_pages` patterns.
- **Process:** score each page by keyword/regex hits for each statement type (P&L, balance sheet, cash flow, segment); detect basis words (standalone/consolidated) and unit lines ("₹ in crore", "Rs. lakhs", etc. — patterns in `sections.yaml`).
- **Output:** `list[PageLabel(page, statement_type | None, basis, unit_hint, score)]`. Pages below `statement_page_min_score` are unlabeled.

### `parsing/pdf_tables.py`
- **Input:** labeled statement pages.
- **Process:** extract tables with `pdfplumber`; identify the label column and period columns from header rows; clean numbers (commas, `(1,234)` → `-1234`, `—`/`-` → null); attach `unit_hint` as scale; compute a confidence score (header found, column count consistent, numeric ratio).
- **Output:** `list[ParsedFact]` with `extractor='pdf_table'|'ocr'` (+ `ocr_backend` when `ocr`), `label_raw`, `concept_raw = None`, `source_loc = page:row:col`, `confidence`. Tables below `min_table_confidence` → `review_queue` (`low_confidence_table`), facts still stored with their confidence.

### `parsing/runner.py`
- **Input:** filings with status `fetched`.
- **Process:** route by format: XBRL → `xbrl_parser` (+ text blocks); PDF → `pdf_text`, then `page_classifier` + `pdf_tables` for results PDFs (annual reports: text only, tables later if needed). Write `parsed_facts`; set status `parsed` or `failed`.
- **Output:** per-filing counts in `jobs`.

### `parsing/cli.py`
- `finrag parse run [--filing ID]`, `finrag parse show --filing ID` (print facts, first N).

### `config/sections.yaml` (create the statement part here; M08 adds narrative sections)
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
7. Golden (if present): totals parsed from a real XBRL file match `golden_values.yaml`.

## Out of scope
Concept mapping, unit conversion to rupees, deduplication across filings (M04/M05).
