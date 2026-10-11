# M10 — Scheduler, Refresh and Alerts

**Package:** `src/finrag/scheduler/` · **Depends on:** M02–M09 · **Tests:** `tests/m10_scheduler/`

## Purpose
Keep the data current with minimal human interaction: detect new filings, run the pipeline only for what changed, recompute what depends on it, and alert the user.

## Key decisions
- **No long-running daemon.** `finrag refresh` is a single idempotent command. The OS scheduler (cron on Linux/macOS, Task Scheduler on Windows) runs it. The command itself decides whether it's time to poll, based on `settings.scheduler`.
- **Network polling only after access terms are confirmed.** Until then, refresh processes the inbox and any backfills.
- **Recompute only what's affected**, using `facts.dependencies.affected`.

## Files

### `scheduler/state.py`
- **Process:** read/write last-poll time per source and ISIN (stored in `jobs` with `job_type='poll'`); `is_due(now)` using season months and intervals from config.

### `scheduler/pipeline.py`
- **Input:** ISIN (or list), mode.
- **Process:** ordered stages, each idempotent: ingest → parse → normalize/facts (+ derivation) → validate (affected periods) → metrics (affected periods) → narrative index (new annual reports). Each stage records a `jobs` row; a stage failure stops later stages for that ISIN only.
- **Output:** `PipelineReport` per ISIN: new filings, restatements flagged, new blocks, metrics recomputed.
- **Command:** `finrag pipeline run [--isin X] [--backfill]`.

### `scheduler/refresh.py`
- **Process:** `watchlist.sync` → backfill newly added ISINs → if due, incremental ingest for all active ISINs → pipeline for ISINs with new filings → alerts.
- **Command:** `finrag refresh [--force]`.

### `scheduler/alerts.py`
- **Input:** `PipelineReport`s.
- **Process:** create `alerts` rows for: new filing processed; restatement flagged; validation block appeared; review-queue items opened; ingestion failures. Send to each channel in `alerts.channels`: `log` (always), `email` (SMTP from config, password from env).
- **Output:** alert rows with `sent_channels_json`. One digest per refresh run, not one message per item.

### `scheduler/cli.py`
- `finrag refresh`, `finrag pipeline run`, `finrag alerts list`, `finrag schedule print-cron` (prints the cron line / Task Scheduler command for the user's OS; does not install it).

## Tests and acceptance criteria
1. Adding an ISIN to the watch list then running `refresh` → backfill runs for that ISIN only.
2. Running `refresh` twice with no new files → no stage does work beyond checks; no alerts.
3. A new filing that restates Q1 → Q1-dependent derived facts, validations and metrics recomputed; untouched periods are not.
4. `is_due` respects season months and intervals (fake clock).
5. A failing stage for one ISIN doesn't stop others; alert raised.
6. Email channel with missing password → clear error in log; `log` channel still works.

## Out of scope
Installing OS scheduler entries, push notifications, web hooks.
