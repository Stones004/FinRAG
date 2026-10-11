# M06 — Validation

**Package:** `src/finrag/validation/` · **Depends on:** M05 · **Tests:** `tests/m06_validation/`

## Purpose
Check the internal consistency of each company-period-basis before metrics are computed. Results are `pass`, `warn`, `block` or `skip` (inputs missing). A `block` stops dependent metrics; the reason is always stored and shown.

## Inputs
- Current view facts (M05).
- `config/validation_rules.yaml` (owned by this module).

### `config/validation_rules.yaml` structure
```yaml
version: 1
rules:
  - id: bs_balances
    description: Total assets equal total equity and liabilities
    type: equality                       # equality | sum | sign | cross_statement | range
    left: total_assets
    right: total_equity_and_liabilities
    period_kind: instant
    severity_on_fail: block
    blocks_concepts: [total_assets, total_equity, total_liabilities]
  - id: pl_total_income
    description: Revenue + other income = total income
    type: sum
    parts: [revenue, other_income]
    total: total_income
    severity_on_fail: warn
  - id: cash_ties_to_bs
    description: Closing cash in cash flow equals cash and equivalents on balance sheet
    type: cross_statement
    left: closing_cash
    right: cash_and_equivalents
    period_kind: instant_at_period_end
    severity_on_fail: block
    blocks_concepts: [closing_cash, cfo, cash_and_equivalents]
  - id: revenue_non_negative
    description: Revenue is not negative
    type: sign
    concept: revenue
    expected: non_negative               # non_negative | non_positive
    severity_on_fail: warn
  - id: tax_rate_plausible
    description: Effective tax rate within a plausible range
    type: range
    numerator: tax_expense
    denominator: profit_before_tax
    min: -0.5
    max: 0.6
    severity_on_fail: warn
```
Tolerances come from `settings.validation` (both relative and absolute must be exceeded to fail). Rule `sectors:` limits a rule to sector types (e.g. skip some for banks).

## Files

### `validation/rules.py`
- **Does:** pydantic models per rule type; load + validate the YAML; unknown rule type → load error.

### `validation/checks.py`
- **Does:** one pure function per rule type: `(rule, facts, tolerance) -> CheckResult(status, details)`. Missing input → `skip` with the missing concept names.

### `validation/engine.py`
- **Input:** isin, list of (period, basis) — default: all periods with facts, or the `AffectedSet` from M05.
- **Process:** run applicable rules; write `validation_results` with a `run_id`; compute the set of **blocked concepts** per (isin, period, basis).
- **Output:** `ValidationReport`; `is_blocked(isin, concept, period, basis) -> (bool, reasons)` used by M07.

### `validation/cli.py`
- `finrag validate run --isin X`, `finrag validate show --isin X --period "FY25"`

## Tests and acceptance criteria
1. Balanced fixture → `pass`.
2. Difference under tolerance → `pass`; over → `block`, and `is_blocked` returns the rule ID as reason.
3. Missing input → `skip`, listing what's missing (not `fail`).
4. Sector filter: rule marked `sectors: [general]` is not run for a bank.
5. Re-running for the same data writes a new `run_id` and identical statuses.
6. Rules are loaded from YAML only; adding a new `sum` rule needs no code change (test adds one in a temp config).

## Out of scope
Fraud/shenanigans signals (deferred). This module checks consistency, not quality of earnings.
