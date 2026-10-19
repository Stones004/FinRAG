# FinRAG — Master Build Document

> This is the single source of truth for building FinRAG. Read it fully before starting any module, and re-read the relevant sections before each module. If anything in a module spec conflicts with this document, this document wins. If something is ambiguous, stop and ask the user — do not guess.

---

## 1. Purpose

FinRAG helps one user (initially) understand the financial statements of Indian listed companies on their personal watch list, across 3–5 years of history, and keeps that understanding current as new filings arrive.

It answers questions such as:
- "What was operating margin in Q1 FY26 and how did it compare to Q1 FY25?"
- "Why did operating margin fall in FY25?" (numbers + management's explanation, with citations)

The later goal (deferred) is to flag possible financial shenanigans. Everything in v1 is designed so that the signal engine can be added as queries over stable data.

### 1.1 In scope for v1
- Indian companies listed on NSE/BSE, on the user's watch list (grows over time).
- Ind AS filings, roughly FY2021 onward (3–5 years, configurable).
- Quarterly/half-yearly/annual financial results (XBRL preferred, PDF fallback).
- Narrative sources (decision 2026-10-13): earnings call transcripts (PDF) as the primary source for quarterly "why" questions; the XBRL text blocks of results filings (notes to the results, audit-opinion declaration, segment notes); and the notes and auditor's-report pages of the quarterly/annual results PDFs. **Annual reports are kept in the raw store but not indexed in v1** (deferred; the whitelist design in M08 is kept for the shenanigans layer).
- Derived metrics from a declarative registry.
- Question answering with citations to facts and pages.
- Periodic refresh, with alerts if access terms allow.
- Single-user local app.

### 1.2 Out of scope for v1 (do not build)
- Shenanigans signal engine (tables are reserved, logic deferred — see §12).
- Multi-user, auth, hosting.
- Buy/sell recommendations of any kind.
- Pre-Ind AS (older Indian GAAP) mappings.
- SME platform listings, REITs, InvITs, ETFs, mutual funds.
- Bank/NBFC/insurance-specific metric sets beyond what the registry's `applicable_sectors` excludes (they are ingested and stored, but only metrics marked applicable are computed).

---

## 2. Non-negotiable principles

1. **The LLM never computes numbers.** All values come from the deterministic engine. The LLM only explains values it is given.
2. **Every number and every claim is traceable.** Each fact links to a filing, and each filing to a raw file with a hash. Each narrative chunk links to a filing and page.
3. **Raw data is immutable.** Downloaded files are never modified or deleted by the system.
4. **The fact store is append-only.** Restatements add rows; nothing is overwritten. "Current view" is a query.
5. **Nothing is dropped silently.** Unmapped items go to a review queue. Uncomputable metrics are stored as "not computed" with a reason.
6. **No guessing.** Missing data, ambiguous intent, or failed validation produce an explicit message, not a best guess.
7. **No hardcoded tunables.** Every threshold, path, model name, URL, tolerance and interval lives in `config/settings.yaml`. Domain lists (taxonomy, metrics, synonyms, section headings, validation rules) live in their own registry YAML files whose paths are in `settings.yaml`.
8. **Money is never a float.** Use `decimal.Decimal` in Python; store as TEXT in SQLite.
9. **Every module is testable in isolation** with fixtures and no network access.

---

## 3. Working rules for Claude Code

1. Build **one module at a time**, in the order in §9. Do not create files belonging to a later module.
2. Before writing code for a module, read `docs/modules/<module>.md` and restate in 5–10 lines what you will build. Wait for the user's go-ahead.
3. A module is **done** only when:
   - every file listed in its spec exists and does what the spec says,
   - `pytest tests/<module_dir>` passes,
   - `pytest` for all previously completed modules still passes,
   - `ruff check` passes,
   - the module's acceptance criteria are each shown passing to the user,
   - `docs/PROGRESS.md` is updated (module, date, what was built, deviations from spec).
4. Stop after each module and wait for the user to confirm before starting the next.
5. If a spec is wrong or incomplete, propose a change to the spec file first, get approval, then code.
6. **Never invent XBRL concept names, URLs or file formats.** Where a spec says "populate from real data", use real files the user provides, or leave a clearly marked TODO and tell the user.
7. Read config only through `finrag.core.config`. No module reads YAML directly or uses literal tunables.
8. No network calls in tests. Mock adapters and the LLM client.
9. Keep functions small, typed (type hints everywhere), and documented with a one-line docstring stating input → output.

---

## 4. Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | |
| Packaging | `pyproject.toml`, `src/` layout, package name `finrag` | |
| Config | YAML + `pydantic` v2 models | Validated at startup; fail fast on bad config |
| Database | SQLite via SQLAlchemy 2.x Core | Schema written portably for a later Postgres move |
| Dataframes | `pandas` (only at edges: parsing, UI) | Core logic uses dataclasses/pydantic |
| XBRL | `lxml` | Exchange XBRL instance files are plain XML |
| PDF text | `pymupdf` (fitz) | Page-level text with coordinates |
| PDF tables | `pdfplumber`; OCR fallback via `OcrBackend`: `pytesseract` (default) or Chandra through an OpenAI-compatible vision endpoint | OCR only for scanned pages; backend chosen by `parsing.ocr.backend` |
| Fuzzy match | `rapidfuzz` | |
| Keyword search | `rank_bm25` | |
| Embeddings | `sentence-transformers` (local model) | Model name in config |
| Vector index | `numpy` brute force | Watch-list scale is small; FAISS dropped at the M08 review |
| LLM | Any OpenAI-compatible local server over HTTP via `httpx`; LM Studio by default | Behind an interface; swappable. Ollama works at its own `/v1` URL |
| CLI | `typer` | Single entry point `finrag` |
| UI | Streamlit | Single-user |
| Tests | `pytest`, `pytest-mock` | |
| Lint/format | `ruff` | |
| Logging | stdlib `logging`, JSON lines to file | |

**Target machine:** RTX 4050 laptop (6 GB VRAM), 16 GB RAM, Windows. Only one GPU model is loaded at a time (OCR during ingestion, LLM at query time). The embedding model runs on CPU (`narrative.embedding_device: cpu`).

Do not add other dependencies without asking.

---

## 5. Repository layout

```
finrag/
├── CLAUDE.md                     # pointer to this doc + working rules summary
├── pyproject.toml
├── config/
│   ├── settings.yaml             # ALL tunables
│   ├── watchlist.yaml            # user's companies (identifiers only)
│   ├── taxonomy.yaml             # canonical concepts + source mappings   (M04)
│   ├── metrics.yaml              # metric registry                        (M07)
│   ├── validation_rules.yaml     # validation rule registry              (M06)
│   ├── sections.yaml             # PDF section/statement heading patterns (M03, M08)
│   ├── vocab/common_en.txt       # common English words for the text-quality score (M03)
│   ├── intents.yaml              # question-type patterns, period patterns (M09)
│   └── prompts/v1/               # versioned LLM prompt templates          (M09)
├── docs/
│   ├── MASTER_BUILD.md
│   ├── PROGRESS.md
│   └── modules/M00_foundation.md … M11_ui.md
├── src/finrag/
│   ├── core/          (M00)
│   ├── watchlist/     (M01)
│   ├── ingestion/     (M02)
│   ├── parsing/       (M03)
│   ├── normalize/     (M04)
│   ├── facts/         (M05)
│   ├── validation/    (M06)
│   ├── metrics/       (M07)
│   ├── narrative/     (M08)
│   ├── llm/           (M09)  base, openai_compat (any OpenAI-compatible server), fake
│   ├── query/         (M09)
│   ├── scheduler/     (M10)
│   ├── app/           (M11)
│   └── cli.py         (grows each module)
├── tests/
│   ├── fixtures/                 # synthetic files; no real company data committed
│   ├── golden/                   # user-supplied real filings + hand-checked values (gitignored)
│   └── m00_core/ … m11_ui/
└── data/                         # gitignored
    ├── raw/  processed/  indexes/  reference/  inbox/  logs/
    └── finrag.db
```

---

## 6. Configuration

### 6.1 `config/settings.yaml`
Holds every tunable value: paths, fiscal settings, defaults, tolerances, source settings, model names, retrieval parameters, thresholds, schedules, alert channels, UI options. The starter file is provided; modules add keys only through a spec change.

### 6.2 Registries (data, not code)
| File | Owner | Purpose |
|---|---|---|
| `watchlist.yaml` | M01 | Identifiers the user wants tracked |
| `taxonomy.yaml` | M04 | Canonical concepts and source → canonical mappings |
| `metrics.yaml` | M07 | Metric definitions, formulas, versions, synonyms |
| `validation_rules.yaml` | M06 | Rule definitions and severities |
| `sections.yaml` | M03/M08 | Heading patterns for statements and narrative sections |
| `intents.yaml` | M09 | Question-type patterns and period patterns |
| `prompts/<version>/*.txt` | M09 | LLM prompt templates; a change = new version folder |

Every registry has a pydantic schema in its owning module and is validated at load time.

### 6.3 Access
`finrag.core.config.get_settings()` returns a cached, validated `Settings` object. `finrag.core.config.load_registry(name)` loads and validates a registry by its key under `paths`.

Environment variable overrides are allowed only for secrets (e.g. SMTP password): `FINRAG_<SECTION>__<KEY>`.

---

## 7. Data model

All tables are created in M00 (`core/db.py`) so later modules never alter the schema without a spec change. Columns below are the minimum; types are SQLite types.

### 7.1 Reference
**entities** — `isin TEXT PK, name TEXT, nse_symbol TEXT, bse_code TEXT, sector_type TEXT, default_basis TEXT, status TEXT, added_on TEXT, updated_on TEXT`

**entity_aliases** — `id INTEGER PK, isin TEXT FK, alias TEXT, alias_type TEXT (symbol|code|name|user|isin|scrip_code), valid_from TEXT, valid_to TEXT`

**corporate_events** — `id INTEGER PK, isin TEXT FK, event_type TEXT (rename|merger|demerger|delisting|basis_change|accounting_policy_change|comparative_restatement), event_date TEXT, details TEXT, source TEXT`

### 7.2 Ingestion
**filings** — `filing_id TEXT PK, isin TEXT FK, source TEXT, filing_type TEXT (financial_results|annual_report|earnings_call_transcript), submission TEXT (original|revision), revision_no INTEGER, taxonomy_family TEXT (indas|banking|other), basis TEXT, period_start TEXT, period_end TEXT, period_type TEXT, filed_on TEXT, source_url TEXT, raw_path TEXT, sha256 TEXT UNIQUE, format TEXT (xbrl|pdf), status TEXT (fetched|parsed|normalized|failed|quarantined), error TEXT, created_at TEXT`

### 7.3 Facts
**parsed_facts** — raw values as extracted: `id INTEGER PK, filing_id FK, concept_raw TEXT, label_raw TEXT, period_start, period_end, basis, value TEXT, unit_raw TEXT, scale INTEGER, decimals INTEGER, source_loc TEXT, dimensions_json TEXT NULL (NULL = entity total; otherwise a JSON list of `{axis, member}` pairs, `typed: true` for typed members), statement_type TEXT NULL (PDF/OCR facts: the statement the page was classified as; NULL for XBRL), extractor TEXT (xbrl|pdf_table|ocr), ocr_backend TEXT NULL (tesseract|chandra; set only when extractor = ocr), confidence REAL`

**canonical_facts** (append-only) — `fact_id TEXT PK, isin, concept TEXT, period_start, period_end, period_type, basis, value TEXT, unit TEXT, source_filing_id FK, parsed_fact_id FK, mapping_method TEXT (standard|label|manual), is_derived INTEGER, derivation_json TEXT, filed_on TEXT, created_at, source_class TEXT NOT NULL (xbrl|pdf|ocr|derived)`

**review_queue** — `id INTEGER PK, item_type TEXT (unmapped_concept|ambiguous_basis|low_confidence_table|entity_ambiguous|transcript_link_only|filing_conflict|inbox_unrecognised|normalization_issue|corporate_event_proposal|gap_fill_rejected), payload_json TEXT, occurrences INTEGER, status TEXT (open|resolved|ignored), resolution_json TEXT, created_at, updated_at`

**restatements** — `id INTEGER PK, isin, concept, period_start, period_end, basis, old_fact_id, new_fact_id, abs_diff TEXT, rel_diff TEXT, flagged INTEGER, created_at`

### 7.4 Validation and metrics
**validation_results** — `id INTEGER PK, isin, period_end, period_type, basis, rule_id TEXT, status TEXT (pass|warn|block|skip|gap_fill_rejected), details_json TEXT, run_id TEXT, created_at`

**metric_values** — `metric_value_id TEXT PK, isin, metric_id TEXT, formula_version TEXT, period_start, period_end, period_type, basis, value TEXT NULL, status TEXT (computed|not_computed), reason TEXT, input_fact_ids_json TEXT, run_id TEXT, created_at, details_json TEXT` (v5: machine-readable `code`, `basis_change`, `basis_fallback`, `assumed_zero`, `warnings`, `restatements`; `reason` is a short sentence; see M07)

### 7.5 Narrative
**chunks** — `chunk_id TEXT PK, filing_id FK, isin, fiscal_year TEXT, section TEXT, source_type TEXT (transcript|results; annual_report reserved, deferred), period_start TEXT, period_end TEXT, period_type TEXT (copied from the filing at index time; search filters on the exact period), speaker TEXT NULL, speaker_role TEXT NULL (management|analyst|moderator), page INTEGER NULL (NULL for an XBRL text block), seq INTEGER, text TEXT, token_count INTEGER, created_at, meta_json TEXT NULL (basis; transcripts: firm, unit, end_page; PDF chunks: end_page; text blocks: concept)`
`speaker` and `speaker_role` are set only for transcript chunks. Transcript `section` ∈ {`prepared_remarks`, `qa`, `closing_remarks`}.
Embeddings and BM25 indexes live under `data/indexes/`, keyed by `chunk_id`.

### 7.6 Query and operations
**query_log** — `query_id TEXT PK, question TEXT, intent_json TEXT, plan_json TEXT, retrieved_ids_json TEXT, prompt_version TEXT, model TEXT, answer TEXT, cited_ids_json TEXT, postcheck_json TEXT, user_feedback TEXT, created_at`

**jobs** — `job_id TEXT PK, job_type TEXT, isin TEXT NULL, status TEXT, started_at, finished_at, error TEXT, stats_json TEXT`

**alerts** — `alert_id TEXT PK, isin, alert_type TEXT, message TEXT, filing_id NULL, sent_channels_json TEXT, created_at`

### 7.7 Reserved (deferred)
**signals** — `signal_id TEXT PK, isin, signal_type TEXT, period_end, value TEXT, severity TEXT, evidence_json TEXT, run_id, created_at` (create table, no logic.)

---

## 8. Conventions

### 8.1 Identifiers
| Object | Format |
|---|---|
| Entity | ISIN (12 chars) |
| Filing | `FIL-<first 12 hex of sha256>` |
| Canonical fact | `FCT-<ulid>` |
| Metric value | `MET-<ulid>` |
| Chunk | `CHK-<filing_id>-p<page>-<seq>`; `CHK-<filing_id>-x-<seq>` for an XBRL text block (no page) |
| Query | `QRY-<ulid>` |
| Job / run | `JOB-<ulid>`, `RUN-<ulid>` |

Use a small ULID helper in `core/ids.py` (time-sortable). In LLM prompts, real IDs are mapped to short local aliases (`F1`, `M1`, `N1`) and mapped back in the log.

### 8.2 Periods and fiscal year
- Indian fiscal year starts on `fiscal.fy_start_month` (April). FY26 = 2025-04-01 to 2026-03-31.
- `period_type` ∈ {`Q`, `H`, `9M`, `FY`, `INSTANT`}. Balance sheet items use `INSTANT` with `period_start = period_end`.
- Labels: Q1 FY26, H1 FY26, 9M FY26, FY26. All label ↔ date conversion goes through `core/fiscal.py`.

### 8.3 Basis
`consolidated` | `standalone` | `unknown`. Every fact, metric and query carries a basis. Default from `defaults.basis`; the answer always states the basis used.

### 8.4 Money and units
- Stored in rupees (`INR`) as `Decimal` serialized to TEXT, plus the original unit/scale in `parsed_facts`.
- Per-share values: unit `INR_PER_SHARE`. Ratios: `RATIO`. Percentages are stored as ratios (0.15) and formatted at display.
- Display unit from `defaults.display_unit` (crore).

### 8.5 Known Indian filing behaviour (design for it)
- Financial results are typically quarterly for P&L; the **balance sheet and cash flow statement are generally disclosed half-yearly and annually**, not every quarter. Do not assume quarterly cash flow exists. Derive only what the inputs support.
- Cumulative figures (H1, 9M, FY) are stored as reported. Discrete periods are derived (e.g. H2 = FY − H1, Q4 = FY − 9M) only when not reported directly, and marked `is_derived`.
- Comparatives in later filings may restate earlier periods.
- Standalone and consolidated are often filed separately.
- Verified on real filings on 2026-10-11 — see `PROGRESS.md` "Findings from real data". Key points: XBRL values are full rupees (don't apply `LevelOfRounding`); Q2/Q4 filings carry the balance sheet and H1/FY cash flow; no prior-period comparatives are tagged; revisions are separate filings; banks use a different XBRL taxonomy.

---

## 9. Module map and build order

| # | Module | Package | Depends on | Spec |
|---|---|---|---|---|
| M00 | Foundation (config, DB, types, fiscal, logging, CLI skeleton) | `core` | — | `modules/M00_foundation.md` |
| M01 | Watch list and entity registry | `watchlist` | M00 | `modules/M01_watchlist.md` |
| M02 | Ingestion and raw store | `ingestion` | M01 | `modules/M02_ingestion.md` |
| M03 | Parsing (XBRL, PDF) | `parsing` | M02 | `modules/M03_parsing.md` |
| M04 | Normalization and taxonomy | `normalize` | M03 | `modules/M04_normalization.md` |
| M05 | Fact store, restatements, derivations | `facts` | M04 | `modules/M05_facts.md` |
| M06 | Validation | `validation` | M05 | `modules/M06_validation.md` |
| M07 | Metrics engine and registry | `metrics` | M06 | `modules/M07_metrics.md` |
| M08 | Narrative indexing and search | `narrative` | M02, M03 | `modules/M08_narrative.md` |
| M09 | Query engine and LLM | `query`, `llm` | M07, M08 | `modules/M09_query.md` |
| M10 | Scheduler, refresh and alerts | `scheduler` | M02–M09 | `modules/M10_scheduler.md` |
| M11 | UI | `app` | all | `modules/M11_ui.md` |

### 9.1 End-to-end pipeline (after M07)
`finrag pipeline run --isin <ISIN>` = ingest → parse → normalize → write facts/derive → validate → compute metrics. Each stage is idempotent and resumable; each records a `jobs` row.

---

## 10. Testing strategy

1. **Unit tests** per file, using fixtures under `tests/fixtures/`.
2. **Synthetic fixtures** are generated by helper scripts (`tests/fixtures/make_*.py`) so no real company data is committed: a small XBRL instance, a small results PDF, a small annual-report PDF.
3. **Golden set** (`tests/golden/`, gitignored): real filings the user adds, plus `golden_values.yaml` with hand-checked values. Golden tests are marked `@pytest.mark.golden` and skipped if the folder is empty.
4. **Acceptance tests** per module, named `test_acceptance_*.py`, map one-to-one to the acceptance criteria in each spec.
5. **Routing test set** (M09): `tests/m09_query/routing_cases.yaml`, ≥50 questions with expected intents.
6. **No network** in any test. Adapters and LLM are mocked.

---

## 11. Logging and observability
- JSON-lines logs to `data/logs/finrag.log` with `run_id`, `module`, `isin`, `event`, `level`.
- Every pipeline stage writes a `jobs` row with counts (fetched, parsed, mapped, unmapped, blocked…).
- `finrag status` prints per-company health: last filing, open review items, blocked periods, last refresh.

---

## 12. Deferred work (design hooks only)
- **Signals engine**: `signals` table exists; metrics registry supports any formula the signals need (DSO, accruals, CFO/NI). No code in v1.
- **Promoter pledging, shareholding, RPT filings**: new source adapters + taxonomy entries later.
- **Annual-report narrative** (decision 2026-10-13): fetched and stored raw, not parsed into chunks or indexed in v1. Re-enable (`narrative.index_annual_reports`) for the shenanigans layer, which needs contingent liabilities, key audit matters, CARO, accounting policies and related-party sections. M08 keeps the sectioner/whitelist design for it.
- **Multi-user**: move SQLite → Postgres; add auth; legal review (SEBI research analyst rules, DPDP Act) before any public release.

---

## 13. Glossary
- **Canonical concept**: FinRAG's internal name for a line item (e.g. `revenue`), defined in `taxonomy.yaml`.
- **Basis**: consolidated or standalone.
- **Current view**: the latest-filed value for each (isin, concept, period, basis).
- **Block**: a validation failure that prevents dependent metrics from being computed.
- **Review queue**: items the system could not resolve automatically and that need a one-time human decision.
