# M07 — Metrics Engine and Registry

**Package:** `src/finrag/metrics/` · **Depends on:** M06 · **Tests:** `tests/m07_metrics/`
**Status:** built 2026-10-13 (rewritten to match the build; decisions from the plan review are marked).

## Purpose
Compute derived metrics deterministically from canonical facts, using formulas declared in `config/metrics.yaml`. Every value records its formula version and input fact IDs. If a metric can't be computed, store `not_computed` with a reason. The LLM never sees or applies formulas.

## `config/metrics.yaml`
```yaml
version: 1
metrics:
  operating_margin:
    version: "1.0"
    name: Operating margin
    description: Operating profit as a share of revenue
    synonyms: ["operating margin", "ebit margin", "operating profit margin", "opm"]
    inputs:
      op:  { metric: operating_profit }
      rev: { concept: revenue }
    formula: "op / rev"
    unit: RATIO              # RATIO | INR | DAYS | TIMES | INR_PER_SHARE
    display: percent         # percent | percentage_points | crore | days | times | number
    period_types: [Q, H, 9M, FY]     # or [INSTANT] for a balance-sheet date
    sectors: [general]       # omit = all
    helper: false            # helpers are hidden from synonyms (M09)
    preferred_basis: standalone   # optional: the basis shown first (both are always computed); omit = the company's default basis
    include_opening: false   # true: instant inputs also look at the opening balance's validation results
    assumptions: [...]
    limitations: [...]
```
Input options (exactly one of `concept` / `metric` / `constant`):
- `offset: {years: -1}` or `{periods: -1}` — read the input at an earlier period via `core/fiscal.shift_period` (an INSTANT metric can only be offset by years).
- `at: period_end | period_start` — an instant input (a concept or an INSTANT metric) of a duration metric. Required for such an input, refused elsewhere. `period_start` means the balance the day before the period starts.
- `missing: fail | zero` (concepts only) — default `fail`. Every assumed zero is recorded in `details_json.assumed_zero`.
- `fallback_basis: standalone` *(plan review)* — requested basis consolidated and no consolidated fact: read the standalone fact for the same period. Allowed only for `zero_is_placeholder` concepts (load error otherwise).
- `zero_is_placeholder_while_nonzero: <concept>` *(plan review)* — a stored 0 is "not reported" while that concept is non-zero in the same period. A **derived** fact of the concept is also not used (it inherits the placeholder). Since 2026-10-13 the taxonomy applies the same rule upstream (`profit_attributable_owners`) and the current view hides stored placeholders, so this option is a second line of defence. Both are recorded in `details_json.placeholder_zero`.
- `fallback: {concept, only_if_zero_or_absent: [instant concepts]}` *(plan review)* — when the input is absent, use this concept, only if every listed instant concept is zero or absent at the period end. Recorded in `details_json.fallbacks`.

Formula language: numbers, input aliases, `+ - * /`, unary minus, parentheses, `avg(a, b, …)`, `abs(x)`, `min(a, b, …)`, `max(a, b, …)`. Nothing else. Every formula number other than a declared `constant` should be an obvious part of the arithmetic (`- 1`).

### Starter metrics (v1) — 32 registry entries
General companies (`sectors: [general]` unless noted):

| id | formula | periods | notes |
|---|---|---|---|
| `operating_profit` | `pbt + fin - oi - exc` | Q,H,9M,FY | exc `missing: zero`, sign as filed (negative = loss). **Associates/JV are not touched**: for all three filers PBT − tax + associates + discontinued ops = PAT in every period (M06 rule `pl_profit_after_tax`), so PBT excludes them. A filer that broke the identity would carry that warning |
| `ebitda` | `op + da` | Q,H,9M,FY | |
| `operating_margin`, `ebitda_margin`, `net_margin` | over `rev` | Q,H,9M,FY | |
| `revenue_growth_yoy` | `rev / rev_ly - 1` | Q,H,9M,FY | |
| `pat_growth_yoy` (all sectors) | `(pat - pat_ly) / abs(pat_ly)` | Q,H,9M,FY | **Deviation from the first draft** `pat / abs(pat_ly) - 1`, which is wrong when last year is a loss; identical for a positive last year |
| `operating_margin_change_yoy` | `om - om_ly` | Q,H,9M,FY | display `percentage_points` |
| `associates_share_of_pat` (all sectors) | `assoc / pat` | Q,H,9M,FY | assoc `missing: zero` |
| `profit_owners` (helper, all sectors) | `po` | Q,H,9M,FY | `profit_attributable_owners`, with `zero_is_placeholder_while_nonzero: profit_after_tax` and `fallback` to `profit_after_tax` when NCI is zero/absent at period end |
| `equity_owners` (helper) | `te - nci` | INSTANT | nci `missing: zero` |
| `bank_equity` (helper, bank) | `cap + res` | INSTANT | banks tag no total equity |
| `capital_employed` (helper) | `te + ltb + stb + ll` | INSTANT | `lease_liabilities` has no XBRL tag in any filing: `ll` is always an assumed zero; stated in limitations of `capital_employed` and `roce` |
| `roe` | `po / avg(eq, eq_ly)` | FY | `po` = `profit_owners`, `eq` = `equity_owners` `at: period_end` and the same with `offset: {years: -1}` |
| `roce` | `op / avg(ce, ce_ly)` | FY | |
| `debt_to_equity` | `(ltb + stb) / eq` | INSTANT | |
| `current_ratio` | `ca / cl` | INSTANT | |
| `cfo_to_pat` | `cfo / pat` | H, FY | cash flow is generally not quarterly |
| `fcf` | `cfo - cx - ci` | H, FY | *(plan review)* purchases of PPE **and** intangibles; `ci` `missing: zero` |
| `dso` | `(rec_c + rec_nc) / rev * days` | FY | `days` = `{constant: 365}`; closing receivables (limitation); `rec_nc` `missing: zero` |

Banks (`sectors: [bank]`; company metrics above are `not_applicable` to banks) *(plan review)*:

| id | formula | periods |
|---|---|---|
| `roe_bank` | `po / avg(eq, eq_ly)` with `bank_equity`; shares its synonyms with `roe` (sectors are disjoint; the registry refuses a shared synonym within one sector) | FY |
| `nii` | `ie - ix` | Q,H,9M,FY |
| `cost_to_income` | `opex / (nii + oi)` (NII + other income − opex = the operating profit before provisions the bank reports, checked on HDFC Bank, both bases) | Q,H,9M,FY |
| `credit_cost` | `prov / avg(adv, adv_ly)`, `include_opening` | FY |
| `advances_growth_yoy`, `deposits_growth_yoy` | `x / x_ly - 1` | INSTANT |
| `credit_deposit_ratio` | `adv / dep` | INSTANT |
| `gross_npa`, `net_npa`, `gross_npa_ratio`, `net_npa_ratio`, `cet1_ratio` | pass-through of the **reported** figure, `fallback_basis: standalone` | Q,H,9M,FY |

**Preferred basis** *(review 2026-10-13)*: `nii`, `cost_to_income`, `credit_cost`, `credit_deposit_ratio`, `advances_growth_yoy`, `deposits_growth_yoy` and the five NPA/CET1 metrics have `preferred_basis: standalone`; `roe_bank` and `pat_growth_yoy` have none (consolidated, the group-level default). Both bases are computed and stored; M09 shows the preferred basis by default, says which basis it used and offers the other (PROGRESS finding 37).

Pass-through rule (PROGRESS finding 32): HDFC Bank's consolidated filings tag NPA and CET1 as 0 (not stored), so a consolidated request reads the standalone fact. The value's `basis` is the requested basis; `details.basis_used` and `details.basis_fallback` say standalone, and the reason says so. Ratios are never recomputed (limitation of `gross_npa_ratio`: NPA / balance-sheet advances gives 1.16 % against the reported 1.15 % for HDFC standalone Q4 FY26).

## Files

### `metrics/registry.py`
- **Process:** load + validate `metrics.yaml` (pydantic, strict); every concept must be in the taxonomy and every metric input in the registry; formulas must only use declared aliases and every alias must be used; `at`/`offset`/period-type consistency (a duration cannot feed an INSTANT metric, an input metric must cover the metric's period types); `fallback_basis` only for `zero_is_placeholder` concepts; synonyms may be shared only across disjoint sectors; dependency cycles are reported with their members; metrics are topologically sorted. Every problem is listed in one `MetricsError`.
- **Output:** `MetricRegistry` with `get(id)`, `order()`, `synonyms()` (helpers excluded; value = list of metric IDs, one per sector), `describe(id)`.

### `metrics/evaluator.py`
- **Process:** parse with `ast`, walk only whitelisted nodes (`BinOp` Add/Sub/Mult/Div, `UnaryOp` USub, `Name`, numeric `Constant`, `Call` to `avg/abs/min/max`); evaluate with `Decimal` at `settings.metrics.decimal_precision` (28). Division by zero (including 0/0) → `EvalError('division_by_zero')`. Anything else → `FormulaError`.
- **Rule:** never use `eval`/`exec`.

### `metrics/engine.py`
`run_metrics(isin, basis=None, periods=None, affected=None, registry=None, engine=None) -> MetricRunReport`. For each basis with facts, each metric in dependency order and each period with facts (durations: Q/H/9M/FY with any fact; instants: dates with a balance-sheet fact):
1. sector and period-type applicability → `not_applicable` (a duration metric is not evaluated on an instant and vice versa);
2. resolve inputs through the M05 current view (offsets, instants, fallbacks, helper results). **If nothing the metric reads at its own period exists, no row is written** (nothing to report); a rejected placeholder counts as something reported;
3. blocked input (`validation.engine.is_blocked`, with `include_opening` for `credit_cost`, `roe`, `roce`; helper-metric inputs inherit through their own concept checks and the opening balance is covered by the `offset` input) → `blocked_by_validation`;
4. missing input → `missing_input` (aliases and periods listed);
5. a recorded `merger`/`demerger` between the input periods (some input period ends before the event date and another on/after it) → `corporate_event_break`;
6. evaluate → `computed` (`division_by_zero` → `not_computed`);
7. **basis change:** the same straddle rule for `accounting_policy_change` and `comparative_restatement` events of the company (an event recorded for another basis is ignored): the value is `computed` and `details.basis_change` lists the events (id, type, date, citation). Applies to every multi-period metric, including those built on helper results; **like-for-like (2026-10-20):** when the event is a `comparative_restatement` that explains the comparatives of a native-text PDF filing (`details.pdf_filing_id`, `linked_filings`, `bases`), and that filing reports the metric's own period and prints an earlier input period again with a different value, the formula is also evaluated on those printed figures: `details.like_for_like = {headline_like_for_like: false, event, restated_value, inputs[{alias, concept, period, original_value/fact_id, restated_value/fact_id, restated_filing_id, restated_page}]}`. The headline value stays the one on the originally filed base. Only inputs read straight from facts are swapped; nothing is stored without a covering event;
8. **warnings:** `warn` results (`warnings_for`) on any input concept and period are in `details.warnings`; **flagged restatements** (M05 `restatements.flagged = 1`) of an input fact are in `details.restatements` (old and new value, IDs). Neither changes the status.

Rows are **append-only and written only on change**: a row is inserted when status, value, reason, input fact IDs or details differ from the newest row of the same (metric, formula version, period, basis); otherwise it counts as `unchanged`. A new formula version always writes new rows and leaves the old ones.
Every run is a `jobs` row (`metrics_run`). `periods` limits the rows written; `affected` (M05 `AffectedSet`) limits them to the affected periods plus the periods that read them through an offset.

### `metrics/models.py`, `metrics/query.py`
- `MetricValue` (decoded row): `value`, `status`, `reason`, `details`, `input_fact_ids`, `basis`, `basis_used`, `basis_change`, `warnings`, `code`.
- `get_metric(isin, metric_id, period, basis)`, `get_metric_series(isin, metric_id, basis, period_type, n)` (one value per period, oldest first), `latest_values(isin, metric_id, basis=None)`. `settings.metrics.version_policy`: `latest` reads the newest row of the key; `pinned` reads the version in `settings.metrics.pinned_versions` (a metric without a pin follows `latest`). A duration request for an INSTANT metric (FY26 `current_ratio`) is mapped to the balance-sheet date by M09.

### `metrics/cli.py`
`finrag metrics run --isin X [--basis]`, `finrag metrics show --isin X --metric id [--period "FY26" | "2026-03-31"] [--basis]`, `finrag metrics list`.

## `metric_values` (schema v5)
`details_json TEXT` was added in v5 (migration: one `ALTER TABLE`). `reason` is a short human-readable sentence; the machine-readable parts are in `details_json`:
`code` (`computed | not_applicable | blocked_by_validation | missing_input | corporate_event_break | division_by_zero`), `basis_requested`, `basis_used`, `basis_fallback`, `basis_change` (events), `assumed_zero` (alias, concept, period end), `fallbacks`, `placeholder_zero`, `warnings` (rule, period end, concept), `restatements`, `missing`, `blocked_by`, `events`.

## Tests and acceptance criteria (all demonstrated)
1. Each starter metric matches a hand-computed value (synthetic company, one test per metric; real Gabriel/Netweb/HDFC values in `test_golden_m07.py`).
2. Evaluator rejects `__import__('os')`, attribute access, unknown names, `**` and the rest of Python.
3. Cycle in the registry → load error naming the cycle. 4. Unknown concept or metric → load error.
5. Blocked input → `not_computed` with the rule ID (and an opening-balance block blocks `roe`). 6. Missing last-year revenue → `missing_input` naming `rev_ly`; the first year's ROE needs the year before.
7. Changing a formula version → new rows; old rows unchanged. 8. A new metric in a temp registry computes with no code change. 9. Results identical across runs; unchanged rows not rewritten.
10. YoY metrics straddling a recorded accounting policy change or restatement are `computed` with `details.basis_change`; both periods on one side of the event are not flagged; an event for another basis is ignored; a merger gives `corporate_event_break`.
11. A `warn` result on an input concept is returned with the value and does not change `status`.
12. Real data (golden): Gabriel FY26 `fcf` and `cfo_to_pat` use the revised operating CF 3,452.74 mn and link the flagged restatement (intangibles capex 1,029.90 → 309.74 mn); Netweb FY26-vs-FY25 metrics are flagged with event 1; HDFC company metrics are `not_applicable`, bank equity = capital + reserves, NPA/CET1 fall back to standalone with the four-quarter gross NPA % trend; Gabriel Q1 FY27 `associates_share_of_pat` = 427.43 / 1,081.26 and 0 earlier; `operating_profit` ties to `revenue − (total expenses − finance cost)`.

## Out of scope
Shenanigan signals (additional registry entries + a separate signal module later).
