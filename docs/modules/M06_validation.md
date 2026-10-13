# M06 — Validation

**Package:** `src/finrag/validation/` · **Depends on:** M05 · **Tests:** `tests/m06_validation/`

## Purpose
Check the internal consistency of each company-period-basis before metrics are computed. Results are `pass`, `warn`, `block` or `skip` (inputs missing). A `block` stops dependent metrics; the reason is always stored and shown. A `warn` blocks nothing, but it is shown wherever the number is shown.

## Inputs
- Current view facts (M05) for every rule except `cross_source`, which reads every stored version.
- `config/validation_rules.yaml` (owned by this module) and `settings.validation`.

### `config/validation_rules.yaml` structure
Rule types: `equality | sum | sign | cross_statement | range | cross_period | cross_source`. Common fields: `id`, `description`, `type`, `severity_on_fail` (`warn` default | `block`), `sectors` (omit = every sector type), `blocks_concepts` (required for `block`, forbidden for `warn`). Every concept name is checked against `taxonomy.yaml` at load, and a rule's concepts must share one period kind (`cross_statement` and `cross_period` work on instants).
```yaml
version: 1
rules:
  - id: bs_balances
    type: equality                       # left = right at the same period
    left: total_assets
    right: total_equity_and_liabilities
    period_kind: instant                 # optional; checked against the taxonomy
    sectors: [general, nbfc]
    severity_on_fail: block
    blocks_concepts: [total_assets, total_equity_and_liabilities, total_equity, total_liabilities]
  - id: pl_profit_before_tax
    type: sum                            # total = parts + optional_parts (absent = 0) - minus
    parts: [total_income]
    optional_parts: [exceptional_items]
    minus: [total_expenses]
    total: profit_before_tax
  - id: pl_profit_after_tax
    type: sum                            # PBT - tax + associates/JV + discontinued ops = profit for the period
    parts: [profit_before_tax]
    optional_parts: [share_of_associates_jv, profit_discontinued_ops]
    minus: [tax_expense]
    total: profit_after_tax
  - id: revenue_non_negative
    type: sign                           # expected: non_negative | non_positive
    concept: revenue
    expected: non_negative
  - id: tax_rate_plausible
    type: range                          # numerator / denominator within [min, max]; zero denominator = skip
    numerator: tax_expense
    denominator: profit_before_tax
    min: -0.5
    max: 0.6
  - id: cash_ties_to_bs
    type: cross_statement                # as equality; period_kind instant_at_period_end
    left: closing_cash
    right: cash_and_equivalents
    severity_on_fail: warn
  - id: opening_cash_equals_prior_closing
    type: cross_period                   # opening at a date = closing at that same date
    opening: opening_cash
    closing: closing_cash
    severity_on_fail: block
    blocks_concepts: [opening_cash, closing_cash]
  - id: xbrl_vs_pdf
    type: cross_source
    identities: [ ... ]                  # sum-style definitions the classifier tests (see below)
```
Banks have their own rules (`bank_bs_balances`, `bank_pl_total_income`) with `sectors: [bank]`, using the bank concepts (`bank_interest_earned`, `bank_deposits`, ...). A rule runs at every period where at least one of its inputs has a fact; a missing input gives `skip` and names the missing concepts.

Tolerances come from `settings.validation` (`relative_tolerance`, `absolute_tolerance_inr`): a check fails only if **both** are exceeded. The relative difference is taken against the larger of the two magnitudes; the absolute (rupee) tolerance applies to INR values only.

### `settings.validation.cross_source` (added in M06)
| key | meaning |
|---|---|
| `rounding_inr` (10,000) | a XBRL/PDF difference up to this is rounding of the printed figure, not a finding |
| `min_shifted_keys` (3) | at least this many differing lines make a consistent shift rather than a one-cell slip |
| `decimal_shift_max_power` (3) | PDF value = XBRL value x 10^k, k = 1..this, reads as a decimal point gained or lost |

## Files

### `validation/rules.py`
- **Does:** pydantic models per rule type (a discriminated union on `type`); load + validate the YAML against the taxonomy; unknown type, unknown concept, block without `blocks_concepts`, mixed period kinds, duplicate ids → `RulesError` listing every problem.

### `validation/checks.py`
- **Does:** one pure function per rule type: `(rule, facts at one period, tolerance) -> CheckResult(status, details)`. `details` always carries the values, the gap and `concepts` (the concepts the result is about; `warnings_for` matches on it).

### `validation/cross_source.py` (added in M06)
- **Does:** the `cross_source` comparison and its classifier (below).

### `validation/events.py` (added in M06)
- **Does:** queue a `corporate_event_proposal` review item for a restated-comparative cluster; `confirm_event` records the event; `recorded_events` lists them.

### `validation/engine.py`
- **Input:** isin, optional list of `PeriodKey` (default: all periods with facts, or the `AffectedSet.periods` from M05).
- **Process:** `evaluate_isin` runs the applicable rules (read-only); `run_validation` appends the rows to `validation_results` under a new `run_id`, records a `jobs` row and queues event proposals.
- **Results are append-only.** The latest row per (isin, period_end, period_type, basis, rule_id) is the current result. `period_end` + `period_type` identify the period (no `period_start` column).
- **Output:** `ValidationReport` (`results`, `counts()`, `blocked()`, `proposals`).
- `is_blocked(isin, concept, period, basis) -> (bool, rule ids)`: a concept is blocked when a latest `block` result of a rule listing it in `blocks_concepts` applies to the period. A duration result applies to its own period. An instant result (balance sheet, cash) applies to a duration through its **end date** only; an opening balance (the target of a taxonomy `opening_concept`, i.e. `opening_cash`) is looked up at the **day before the period starts** only. `include_opening=True` (for a metric that averages opening and closing balances) adds the opening date to a plain concept. A plain FY `closing_cash` request never sees a block at the opening date.
- `warnings_for(isin, concept, period, basis)`: latest `warn` results whose `details.concepts` name the concept. Consumers show them next to the number.

### `validation/cli.py`
- `finrag validate run --isin X`, `finrag validate show --isin X [--period "FY25"] [--status warn]`
- `finrag review confirm-event <item> [--event-type accounting_policy_change|comparative_restatement] [--event-date YYYY-MM-DD] [--citation "..."]`

## Rules with special behaviour

### `cross_period` (opening cash = prior closing cash)
M04 stores a cash-flow statement's opening balance as `opening_cash` dated the day before the period starts, which is the date of the earlier period's `closing_cash`. The rule compares the two at that date. On failure it blocks `opening_cash` and `closing_cash` **at that date only**. No closing at that date (the first year on file) is a `skip`.

### `cash_ties_to_bs`
`warn` for everyone, with the gap in `details`. Real case: Gabriel's cash-flow closing cash is 15-38 mn below its balance-sheet cash in all three periods (cause not confirmed; see PROGRESS finding 34). Banks have no `cash_and_equivalents` concept, so it skips.

### `cross_source` rule type
M05 stores XBRL and PDF/OCR facts for the same key and only applies precedence in the current view; it never compares them. M06 owns that check and never changes a fact: it is a finding, not a restatement.

1. For each PDF/OCR filing, compare every key that also has an XBRL value (the current XBRL version). A difference up to `rounding_inr` is rounding (per-share and ratio concepts: relative `validation.relative_tolerance`).
2. Every key that differs by more than rounding is classified together with the other differing keys of the same **PDF filing and period end** (one cluster: the FY, quarter and balance-sheet columns of one date).
3. The rule warns for a period only if a key there exceeds `validation.*` (both tolerances); the classification is attached either way (a restatement can sit below the tolerance).
4. Each line gets the OCR-error patterns it shows: `decimal_shift` (a decimal point gained or lost), `digit_substitution` (one wrong digit or one adjacent swap), `low_confidence` (confidence at or below `parsing.pdf.format_outlier_confidence`).
5. The rule's `identities` (sum-style: income, profit before tax, profit after tax, expense lines, equity, balance sheet) are tested on both sources for the periods that include a differing line. An identity that fails on the XBRL side is ignored (not a fair test).
6. Classification, never a guess:
   - `restated_comparative_likely`: at least `min_shifted_keys` lines differ, at least one testable identity holds in the PDF, none breaks, and no line shows a decimal shift or low confidence;
   - `ocr_error_likely`: fewer than `min_shifted_keys` lines, every one with an OCR-error pattern, and no testable identity that includes them holds in the PDF;
   - `unclear`: everything else. The reasons are stored.

Evidence (differing keys with values, filings and signals; identities with whether they hold on each side; the reasons) is stored in `details_json`.

### Corporate-event proposals
A `restated_comparative_likely` cluster queues one review item `corporate_event_proposal` (once per isin, basis, PDF filing and restated period end; a decided item is never queued again). The payload holds the evidence and the affected keys, `proposed_event_type: comparative_restatement` and `proposed_event_date` = the day after the restated period end. M05's values are **not** changed. `finrag review confirm-event` records a `corporate_events` row (`comparative_restatement`, or `accounting_policy_change` when the company's notes say so) and resolves the item; `finrag review ignore` declines it. M07 flags metrics that straddle a recorded event (see M07).

## Tests and acceptance criteria
1. Balanced fixture → `pass`.
2. Difference under tolerance → `pass`; over → `block`, and `is_blocked` returns the rule ID as reason.
3. Missing input → `skip`, listing what's missing (not `fail`).
4. Sector filter: rule marked `sectors: [general]` is not run for a bank.
5. Re-running for the same data writes a new `run_id` and identical statuses.
6. Rules are loaded from YAML only; adding a new `sum` rule needs no code change (test adds one in a temp config).
7. A deliberately broken synthetic case per rule type gives `block` or `warn` with the right reason, and (with a test-only metric→input map) its dependent metrics would be blocked; warnings block nothing.
8. `cross_source` classifies a restated comparative, an OCR slip and an unclear case correctly; a restated comparative queues a proposal, confirming it records the event, and no fact changes.
9. A plain closing-balance request does not see a block at the period's opening date; `opening_cash` does; `include_opening=True` sees both. `pl_profit_after_tax` includes the associates and discontinued-operations terms.
10. Golden (real data, read-only): balance sheets balance, opening = prior closing, income sums hold, Netweb FY25 is a restated comparative and Netweb Q4 FY26 PBT an OCR error.

## Out of scope
Fraud/shenanigans signals (deferred). This module checks consistency, not quality of earnings. Reading the company's notes is not done by the rule (the proposal carries the evidence; the person confirming cites the note).
