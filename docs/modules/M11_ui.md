# M11 — UI (Streamlit, single user)

**Package:** `src/finrag/app/` · **Depends on:** all · **Tests:** `tests/m11_ui/`

## Purpose
A local app to see the watch list, browse statements and metrics, ask questions, and clear the review queue. The UI reads only through module APIs (`facts.current_view`, `metrics.query`, `query.service`, etc.) — never raw SQL.

## Files

### `app/main.py`
- **Does:** Streamlit entry; sidebar navigation; loads settings once. Command: `finrag ui` (wraps `streamlit run`).

### `app/pages/watchlist.py`
- **Shows:** each company: name, ISIN, sector type, status, last filing, open review items, blocked periods, last refresh. Buttons: run pipeline for one company, run refresh.

### `app/pages/company.py`
- **Input:** selected ISIN, basis toggle, period type (Q / H / FY), years (`ui.default_years_shown`).
- **Shows:** statement tables (rows = canonical concepts, columns = periods) with derived values marked and restated values flagged; metrics table with sparklines; `not_computed` cells greyed with the reason on hover when `ui.show_blocked_metrics`; restatement list; validation results.
- **Each cell:** click → source filing, page/context, mapping method.

### `app/pages/ask.py`
- **Input:** question; optional company selector to pre-fill the entity.
- **Shows:** interpretation line ("Reading this as: operating margin · Q1 FY26 vs Q1 FY25 · consolidated") when `ui.show_interpretation`; clarification options as buttons; numbers block; explanation; citations (filing + page, expandable chunk text); warnings (dropped sentences, defaults applied); thumbs up/down → `query.service.feedback`.

### `app/pages/review.py`
- **Shows:** open review items grouped by type and occurrences. Actions: map unmapped label to a canonical concept (dropdown from taxonomy), choose basis, pick entity for ambiguous names, ignore. Uses `normalize.review_queue.resolve`.

### `app/pages/health.py`
- **Shows:** jobs (last runs, failures), alerts, mapping coverage per company, routing test pass rate (last run), LLM status (`ping`).

### `app/components.py`
- **Does:** shared table/format helpers using `query.formatter`.

## Tests and acceptance criteria
1. Each page's data-loading function (separated from rendering) returns correct structures on a fixture DB.
2. Blocked metric renders with its reason text (tested at data-function level).
3. Resolving a review item from the page function updates the queue and the next pipeline run maps the item.
4. `finrag ui` launches (smoke test with Streamlit's testing utilities).
5. No page imports `sqlalchemy` (grep test).

## Out of scope
Auth, multi-user, hosting, charts beyond simple series.
