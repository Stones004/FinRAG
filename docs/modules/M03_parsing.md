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
- **Process:** `pymupdf` page text; if a page has fewer than `parsing.pdf.min_text_chars_for_native` characters, **or** its text-quality score (share of tokens that are dictionary words / known financial terms) is below `parsing.pdf.min_text_quality`, OCR it. Real case: Netweb's Q4 FY26 results PDF is a scan whose embedded OCR layer reads "prolit", "OPERATINC" (`pytesseract`, `ocr_dpi`, `ocr_lang`) if `ocr_enabled`.
- **Output:** `processed/<filing_id>/pages.jsonl` — one line per page: `{page, text, method: native|ocr, char_count}`.

### `parsing/page_classifier.py`
- **Input:** pages, `config/sections.yaml` → `statement_pages` patterns.
- **Process:** score each page by keyword/regex hits for each statement type (P&L, balance sheet, cash flow, segment); detect basis words (standalone/consolidated) and unit lines ("₹ in crore", "Rs. lakhs", etc. — patterns in `sections.yaml`).
- **Output:** `list[PageLabel(page, statement_type | None, basis, unit_hint, score)]`. Pages below `statement_page_min_score` are unlabeled.

### `parsing/pdf_tables.py`
- **Input:** labeled statement pages.
- **Process:** extract tables with `pdfplumber`; identify the label column and period columns from header rows; clean numbers (commas, `(1,234)` → `-1234`, `—`/`-` → null); attach `unit_hint` as scale; compute a confidence score (header found, column count consistent, numeric ratio).
- **Output:** `list[ParsedFact]` with `extractor='pdf_table'|'ocr'`, `label_raw`, `concept_raw = None`, `source_loc = page:row:col`, `confidence`. Tables below `min_table_confidence` → `review_queue` (`low_confidence_table`), facts still stored with their confidence.

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
5. A page with no text → OCR path chosen (OCR mocked in unit tests).
6. Low-confidence table → review-queue item; facts kept with confidence.
7. Golden (if present): totals parsed from a real XBRL file match `golden_values.yaml`.

## Out of scope
Concept mapping, unit conversion to rupees, deduplication across filings (M04/M05).
