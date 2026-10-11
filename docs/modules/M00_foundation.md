# M00 — Foundation

**Package:** `src/finrag/core/` · **Depends on:** nothing · **Tests:** `tests/m00_core/`

## Purpose
Shared plumbing every other module uses: validated config, database schema, common types, IDs, fiscal-period logic, logging, and the CLI skeleton. No business logic.

## Files

### `pyproject.toml`
- **Does:** package metadata, dependencies from MASTER_BUILD §4, `finrag` console script → `finrag.cli:app`, pytest and ruff config.

### `core/config.py`
- **Input:** `config/settings.yaml`; env vars `FINRAG_<SECTION>__<KEY>` (secrets only).
- **Process:** load YAML → validate with pydantic `Settings` model (one nested model per top-level section) → resolve relative paths against the repo root → apply env overrides → cache.
- **Output:** `get_settings() -> Settings`; `load_registry(name: str) -> dict` (reads `settings.paths.<name>`, returns raw dict; owning module validates); `reset_settings_cache()` for tests.
- **Errors:** missing/invalid keys raise `ConfigError` naming the key.

### `core/db.py`
- **Input:** `settings.paths.db`.
- **Process:** define every table in MASTER_BUILD §7 with SQLAlchemy Core `MetaData`; `init_db()` creates tables if absent; a `schema_version` table records the version; `get_engine()` and a `transaction()` context manager.
- **Output:** engine, table objects importable as `from finrag.core.db import tables`.
- **Rule:** later modules never alter the schema without a spec change.

### `core/types.py`
- **Does:** shared pydantic/dataclass types and enums: `Basis`, `PeriodType`, `FilingFormat`, `FilingStatus`, `ValidationStatus`, `MetricStatus`, `SectorType`, `Period(start, end, type)`, `Money` helpers (`to_decimal`, `decimal_to_text`, `text_to_decimal`).
- **Rule:** no floats for money; `to_decimal` rejects float input.

### `core/ids.py`
- **Input:** prefix, optional seed (sha256 for filings).
- **Process:** ULID generation (time-sortable); deterministic filing IDs from hash; chunk ID builder.
- **Output:** `new_id(prefix) -> str`, `filing_id_from_sha(sha) -> str`, `chunk_id(filing_id, page, seq) -> str`.

### `core/fiscal.py`
- **Input:** dates or labels; `settings.fiscal`.
- **Process:** convert between dates and labels (Q1 FY26 ↔ 2025-04-01..2025-06-30); fiscal year of a date; previous period / same period last year; check whether a period is cumulative (H, 9M, FY); list component periods (FY = 9M + Q4, H1 = Q1 + Q2).
- **Output:** `label_to_period`, `period_to_label`, `fiscal_year_of`, `shift_period(period, years=0, periods=0)`, `components_of(period)`.
- **Must handle:** any `fy_start_month`, not just April.

### `core/logging.py`
- **Input:** `settings.app.log_level`, `settings.paths.logs`, `settings.logging.json_logs`.
- **Process:** configure root logger; JSON-lines file handler + readable console handler; `get_logger(module)`; `bind(run_id=…, isin=…)` context.
- **Output:** loggers writing to `data/logs/finrag.log`.

### `core/jobs.py`
- **Process:** `start_job(job_type, isin=None) -> job_id`, `finish_job(job_id, status, stats, error=None)` writing the `jobs` table.

### `cli.py`
- **Does:** Typer app with commands `init` (create folders + DB), `config show` (print resolved settings, secrets masked), `status` (placeholder, filled later). Each later module adds a sub-command group.

## Tests and acceptance criteria
1. `finrag init` on an empty folder creates `data/` subfolders and a DB containing every table in §7.
2. Invalid settings (e.g. `fy_start_month: 13`) fail with an error naming the key.
3. No module other than `core/config.py` opens a YAML file (enforced by a test that greps `src/`).
4. `to_decimal(0.1)` raises; `to_decimal("0.1")` works; round-trip through TEXT is exact.
5. Fiscal: Q1 FY26 = 2025-04-01..2025-06-30; FY26 = 2025-04-01..2026-03-31; same period last year of Q3 FY26 is Q3 FY25; works with `fy_start_month: 1`.
6. `filing_id_from_sha` is deterministic.
7. A job row is written with start/finish timestamps and stats.

## Out of scope
Any company, filing or metric logic.
