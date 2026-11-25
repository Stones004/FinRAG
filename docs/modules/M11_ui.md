# M11 — UI (Streamlit, single user) — minimal v1

**Package:** `src/finrag/app/` · **Depends on:** M09 (query), M05 (facts) · **Tests:** `tests/m11_ui/`

**Scope decision (2026-10-20):** v1 is the Ask page and the Status page, plus the feedback table. The
other pages of the original design (watch list, company statements and metrics, review-queue resolver,
health) are **deferred**, as are the review-queue actions (map a label, choose a basis, ignore). They are
kept below under "Deferred" so the design is not lost. M11 is built before M10 (scheduler) on request.

## Purpose
A local app on `localhost` to ask questions and see how far the data reaches. It reads through module
APIs only (`query.service`, `facts.status`, `query.feedback`); no page imports `sqlalchemy`.

## Files

### `app/main.py`
- **Does:** Streamlit entry; sidebar choice of page. Command: `finrag ui` (wraps `streamlit run` with
  `ui.server_address` and `ui.port`).

### `app/runtime.py`
- **Does:** `st.cache_resource` objects built once per process: the database connection (with
  `init_db`), the embedding model (warmed with `ui.embedder_warmup_text`) and the `QueryService` built on
  them, so a question does not reload the model weights.

### `app/data.py`
- **Does:** page data without Streamlit: `answer_view` (interpretation line when `ui.show_interpretation`,
  body = the text `finrag ask` prints, without the repeated first line), `clarification_view`,
  `reask_text`, `submit_wrong_answer`, `status_view`.

### `app/pages/ask.py`
- **Shows:** question box; the interpretation line above the answer; a clarification as one button per
  candidate (a click asks again with the candidate's name appended to the question); the answer exactly
  as `finrag ask` lays it out (figures block with basis, "Not like for like" blocks, warnings, evidence
  quotes with source labels and filing ids, sources, notes); a "This answer is wrong" button with a
  free-text note.

### `app/pages/status.py`
- **Shows:** per company: latest period with data, latest period with a usable transcript; open
  review-queue counts by item type.

### `facts/status.py`, `query/feedback.py`
- **Do:** the two queries the pages need (`company_status`, `open_review_counts`) and
  `record_wrong_answer`.

### Schema v7
- Table `feedback(id, query_id, question, answer, intent_json, note, created_at)`. A row is written by
  "This answer is wrong"; `intent_json` is read from the `query_log` row of the answer, whose
  `user_feedback` is set to `correct: false` in the same transaction.

### Settings (`ui`)
`show_interpretation`, `page_title`, `server_address`, `port`, `embedder_warmup_text`,
`feedback_note_max_chars`. No company names in `src/`.

## Tests and acceptance criteria
1. The v6 -> v7 migration creates `feedback` and keeps existing rows.
2. "This answer is wrong" writes question, answer, intent and note; an unknown `query_id` writes nothing.
3. Status data on a fixture DB: latest period with data, latest usable transcript (failed and quarantined
   ones ignored), open review counts by type (resolved items not counted).
4. Streamlit `AppTest`: the Ask page shows the interpretation above the answer; clarification options
   re-ask with the name appended; the feedback button writes a row; the Status page lists the companies.
5. The embedding model and the connection are built once across reruns (`cache_resource`).
6. `finrag ui` starts Streamlit on the configured address and port (subprocess mocked).
7. No file under `app/` imports `sqlalchemy`; no company names in the new source files.

## Known limitation
A clarification choice is passed back as text (the question plus the candidate's name), not as an id. If
the appended name mis-resolves, a service entry point that answers with the chosen id is the fix.

## Deferred (original design, not built)
- `pages/watchlist.py`: each company with ISIN, sector type, status, last filing, open review items,
  blocked periods, last refresh; buttons to run the pipeline or a refresh (needs M10).
- `pages/company.py`: statement tables, metrics with sparklines, blocked metrics greyed with the reason,
  restatement list, validation results, per-cell source (`ui.show_blocked_metrics`,
  `ui.default_years_shown` are reserved for it).
- `pages/review.py`: open review items grouped by type; map a label to a concept, choose a basis, pick an
  entity, ignore (`normalize.review_queue.resolve`).
- `pages/health.py`: jobs, alerts, mapping coverage, routing pass rate, LLM status.
- Thumbs up/down beside the wrong-answer button.

## Out of scope
Auth, multi-user, hosting, charts beyond simple series.
