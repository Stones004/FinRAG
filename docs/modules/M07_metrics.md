# M07 — Metrics Engine and Registry

**Package:** `src/finrag/metrics/` · **Depends on:** M06 · **Tests:** `tests/m07_metrics/`

## Purpose
Compute derived metrics deterministically from canonical facts, using formulas declared in `config/metrics.yaml`. Every value records its formula version and input fact IDs. If a metric can't be computed, store `not_computed` with a reason. The LLM never sees or applies formulas.

## `config/metrics.yaml` structure
```yaml
version: 1
metrics:
  operating_margin:
    version: "1.0"
    name: Operating margin
    description: Operating profit as a share of revenue
    synonyms: ["operating margin", "ebit margin", "operating profit margin", "opm"]
    inputs:
      op:  { metric: operating_profit }            # metrics may use other metrics
      rev: { concept: revenue }
    formula: "op / rev"
    unit: RATIO                                     # RATIO | INR | DAYS | TIMES | INR_PER_SHARE
    display: percent                                # percent | crore | days | times | number
    period_types: [Q, H, 9M, FY]
    sectors: [general]                              # omit = all
    assumptions: ["Operating profit as defined by metric operating_profit"]
    limitations: ["Not comparable across a corporate event"]
```
Input options:
- `concept:` or `metric:` (exactly one)
- `offset: {years: -1}` or `{periods: -1}` — same/previous period via `core/fiscal.shift_period`
- `at: period_end | period_start` — for instants used with duration metrics (e.g. opening equity)
- `missing: fail | zero` — default `fail`; `zero` only for items that are legitimately often absent (e.g. `exceptional_items`, `non_controlling_interest`)

Formula language: numbers, input aliases, `+ - * /`, parentheses, functions `avg(a, b)`, `abs(x)`, `min(a,b)`, `max(a,b)`. Nothing else.

### Starter metrics (v1)
| id | formula (aliases → inputs) | periods | notes |
|---|---|---|---|
| `operating_profit` | `pbt + fin - oi - exc` | Q,H,9M,FY | exc `missing: zero`; assumption: exceptional items sign as normalized in taxonomy |
| `ebitda` | `op + da` | Q,H,9M,FY | |
| `operating_margin` | `op / rev` | Q,H,9M,FY | |
| `ebitda_margin` | `ebitda / rev` | Q,H,9M,FY | |
| `net_margin` | `pat / rev` | Q,H,9M,FY | |
| `revenue_growth_yoy` | `rev / rev_ly - 1` | Q,H,9M,FY | `rev_ly` = revenue `offset: {years: -1}` |
| `pat_growth_yoy` | `pat / abs(pat_ly) - 1` | Q,H,9M,FY | limitation: unstable when last year near zero |
| `operating_margin_change_yoy` | `om - om_ly` | Q,H,9M,FY | unit RATIO, display as percentage points |
| `associates_share_of_pat` | `assoc / pat` | Q,H,9M,FY | assoc = `share_of_associates_jv` (`missing: zero`); added from M06 finding 35 (Gabriel Q1 FY27: 39.5 % of PAT, about 0 before) |
| `equity_owners` (helper) | `te - nci` | INSTANT | nci `missing: zero` |
| `bank_equity` (helper) | `bank_share_capital + bank_reserves_and_surplus` | INSTANT | banks tag no total equity; this is a helper metric, not a taxonomy concept |
| `capital_employed` (helper) | `te + ltb + stb + ll` | INSTANT | ll (lease liabilities) `missing: zero` |
| `roe` | `pat_owners / avg(eq, eq_ly)` | FY | eq = `equity_owners` `at: period_end`; eq_ly = same with `offset: {years: -1}` |
| `roce` | `op / avg(ce, ce_ly)` | FY | ce = helper metric `capital_employed` = equity + borrowings + lease liabilities |
| `debt_to_equity` | `(ltb + stb) / eq` | INSTANT | eq = `equity_owners` |
| `current_ratio` | `ca / cl` | INSTANT | |
| `cfo_to_pat` | `cfo / pat` | H, FY | cash flow is generally not quarterly |
| `fcf` | `cfo - capex` | H, FY | taxonomy normalizes `capex_ppe` to a positive outflow |
| `dso` | `rec / rev * days` | FY | `days` = 365 constant defined in the formula input as `{ constant: 365 }`; uses closing receivables (limitation) |

Receivables and payables are split in the taxonomy (`trade_receivables_current` / `_noncurrent`, `trade_payables_current` / `_noncurrent`): metrics that need the total (DSO, DPO) sum the two (`missing: zero` for the non-current one). Concepts with `sign_convention: outflow_positive` hold positive outflows in both XBRL and (after M04's `pdf_sign`) PDF facts.

Add `constant:` as an input option for fixed numbers so formulas contain no magic literals beyond those declared.

## Files

### `metrics/registry.py`
- **Process:** load + validate `metrics.yaml`; check every input refers to a taxonomy concept or registry metric; detect dependency cycles; topologically sort metrics.
- **Output:** `MetricRegistry` with `get(id)`, `order()`, `synonyms()` (used by M09), `describe(id)`.

### `metrics/evaluator.py`
- **Process:** parse formula with Python `ast`; allow only `BinOp(Add, Sub, Mult, Div)`, `UnaryOp(USub)`, `Name` (declared aliases), `Constant` (numbers), `Call` to the whitelisted functions; evaluate with `Decimal`. Division by zero → `EvalError('division_by_zero')`.
- **Output:** `evaluate(formula, values: dict[str, Decimal]) -> Decimal`.
- **Rule:** never use `eval`/`exec`.

### `metrics/engine.py`
- **Input:** isin, periods, basis (default: all periods with facts, or an `AffectedSet` from M05).
- **Process:** for each metric in dependency order and each applicable period:
  1. sector and period-type applicability → else `not_computed: not_applicable`;
  2. resolve inputs via `facts.current_view` (+ offsets) or earlier metric results;
  3. any input blocked (`validation.engine.is_blocked`) → `not_computed: blocked_by_validation (<rule ids>)`; pass `include_opening=True` for inputs of metrics that average opening and closing balances (`roe`, `roce`), otherwise a balance is judged at the period end only and an opening balance (`opening_cash`) at the day before the period starts;
  4. missing input (and `missing: fail`) → `not_computed: missing_input (<names>)`;
  5. a `corporate_events` merger/demerger between the input periods → `not_computed: corporate_event_break`;
  6. evaluate → `computed`;
  7. **basis change (decided at M06 review):** if the metric's input periods straddle a recorded `accounting_policy_change` or `comparative_restatement` event for the company (some input period ends before the event date and another ends on or after it), the value is still `computed` but flagged `basis_change`: `metric_values.reason` is `basis_change:<event_id>[,<event_id>]` and `MetricValue` carries the events. The answer (M09) and the UI (M11) must show the event(s) next to the number (type, date, citation from `corporate_events.details`). Applies to every YoY or multi-period metric, including those using `offset`. Comparisons must never mix bases silently. M06 does not override M05's values: periods before the event are served as filed (the old basis);
  8. **validation warnings:** `warn` results (M06 `warnings_for`) on any input concept and period are attached to the value and shown wherever the number is shown. Warnings never block.
  Write `metric_values` with `formula_version`, `input_fact_ids_json`, `run_id`.
- **Output:** `MetricRunReport` (computed / not_computed by reason).

### `metrics/query.py`
- **Process:** read latest `metric_values` for (isin, metric, period, basis), respecting `settings.metrics.version_policy`.
- **Output:** `get_metric(...)`, `get_metric_series(...)` — used by M09 and M11.

### `metrics/cli.py`
- `finrag metrics run --isin X`, `finrag metrics show --isin X --metric operating_margin`, `finrag metrics list`

## Tests and acceptance criteria
1. Each starter metric matches a hand-computed value on a fixture company (one test per metric).
2. Evaluator rejects `__import__('os')`, attribute access, unknown names, and `**`.
3. Cycle in registry → load error naming the cycle.
4. Input referring to an unknown concept → load error.
5. Blocked input → `not_computed` with the rule ID in the reason.
6. Missing last-year revenue → `revenue_growth_yoy` `not_computed: missing_input (rev_ly)`.
7. Changing a formula version → new rows with the new version; old rows unchanged.
8. Adding a new metric to YAML (in a temp config) computes with no code change.
9. Decimal precision: results identical across runs.
10. A YoY metric whose periods straddle a recorded `accounting_policy_change` (or `comparative_restatement`) is `computed` with `reason = basis_change:<event id>`; a metric with both periods on the same side of the event is not flagged; a merger/demerger still gives `not_computed`.
11. A `warn` result on an input concept is returned with the value and does not change `status`.

## Out of scope
Shenanigan signals (they will be additional registry entries + a separate signal module later).

## Note from M05 review: bank asset-quality metrics fall back to standalone
HDFC Bank's consolidated filings tag NPA and CET1 as 0 (not stored, `zero_is_placeholder`); the real values exist only on the **standalone** basis (PROGRESS finding 32). For `bank_gross_npa`, `bank_net_npa`, their ratios and `bank_cet1_ratio` the metric layer must fall back to the standalone fact when the requested basis is consolidated, and the result must say so (basis used, and why) in the metric value's `reason` and in the answer. These concepts are never derived (`derivable: false`), so only reported periods exist.
