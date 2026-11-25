# FinRAG

Local-first question answering over the financial filings of a watch list of Indian listed companies.
Filings (NSE/BSE XBRL, PDFs, transcripts) are ingested, parsed, normalised into canonical facts,
validated, turned into metrics, and answered through a query engine with an LLM layer. The build is
split into numbered modules (M00-M11); the full status is in `docs/PROGRESS.md` and the build rules are
in `CLAUDE.md`.

## Install and run

```bash
pip install -e ".[dev]"        # core + tests; extras: ingestion, parsing, narrative, llm, ui
finrag init                    # create folders and the SQLite schema
finrag ask "Profit growth Q1 FY27 for <company>"
pip install -e ".[ui]" && finrag ui   # Streamlit app (Ask and Status pages)
pytest
```

Settings live in `config/settings.yaml`, metric definitions in `config/metrics.yaml`, and query intents in
`config/intents.yaml`. Secrets are read from environment variables only (never commit a `.env`).

## Project status (as of 2026-11-26)

**Last thing built - M11 UI, minimal v1 (2026-10-20).** A local Streamlit app (`finrag ui`) with two pages:
- **Ask:** shows how the question was interpreted, offers clarification buttons, prints the answer exactly as
  `finrag ask` does, and has a "This answer is wrong" button that writes a row to the new `feedback` table
  (schema v7).
- **Status:** per company, the latest period with data, the latest period with a usable transcript, and open
  review-queue counts.

The other M11 pages (watch list, company statements, review-queue resolver, health) are deferred; the design
is kept in `docs/modules/M11_ui.md`. M10 (scheduler) has not been started.

**Most recent dev changes included in the latest push:**
- UI package `src/finrag/app/` (main, runtime, data, `pages/ask`, `pages/status`), `facts/status.py`,
  `query/feedback.py`, and the `finrag ui` command; new `ui.*` settings.
- Growth companion: questions that use change words (rise, fall, growth, ...) now also show the growth
  figure as a "Context" line (`growth_companion` in `metrics.yaml`, `growth_counterparts` in `intents.yaml`;
  planner, numbers block and service updated).
- The `xbrl_vs_pdf` validation warning is no longer repeated when a recorded restatement event already
  explains the difference.
- New tests: `tests/m11_ui/` and `tests/m09_query/test_followup_growth_companion.py`.

**Previous push (2026-10-20, commit `7a09499`):** pdf_comparative, the comparative-restatement event,
like-for-like growth and the transcript default period.

| Module | Status |
|---|---|
| M00 Foundation, M01 Watch list, M02 Ingestion | built, awaiting review |
| M03 Parsing, M04 Normalization, M05 Facts | built, awaiting review |
| M06 Validation, M07 Metrics | built, awaiting review |
| M08 Narrative | built, review follow-up done |
| M09 Query + LLM | built, awaiting review |
| M10 Scheduler | not started |
| M11 UI | minimal v1 built, awaiting review |
