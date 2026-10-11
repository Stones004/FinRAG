# M04 — Normalization and Taxonomy

**Package:** `src/finrag/normalize/` · **Depends on:** M03 · **Tests:** `tests/m04_normalize/`

## Purpose
Map every parsed fact to a canonical concept, convert to rupees, assign period type and basis, and hand clean candidates to M05. Anything that can't be mapped goes to the review queue — never dropped.

## Inputs
- `parsed_facts` for filings with status `parsed`.
- `config/taxonomy.yaml` (owned by this module).

### `config/taxonomy.yaml` structure
```yaml
version: 1
concepts:
  revenue:
    label: Revenue from operations
    statement: profit_and_loss          # profit_and_loss | balance_sheet | cash_flow | other
    period_kind: duration               # duration | instant
    unit: INR                           # INR | INR_PER_SHARE | SHARES | RATIO
    sign: as_reported                   # as_reported | positive | negative
    sectors: [general, nbfc]            # omit = all
    xbrl_concepts: []                   # filled from real filings — DO NOT INVENT
    labels: ["revenue from operations", "income from operations", "net sales"]
  other_income: { ... }
  total_income: { ... }
  # starter list below
```
**XBRL concepts are now known** for the `in-capmkt` taxonomy (PROGRESS finding 4; full lists in `data/inbox/*.xml`; mapping examples in `tests/golden/golden_values.yaml` → `xbrl_concept`). Populate `xbrl_concepts` from those real files. Banks need their own concepts (prefix `bank_`, `sectors: [bank]`), e.g. `bank_interest_earned`, `bank_interest_expended`, `bank_provisions`, `bank_deposits`, `bank_advances`. Ignore dimensioned facts (segments, other-expense breakdown) for entity totals.

**Starter canonical concepts:**
P&L — `revenue`, `other_income`, `total_income`, `cost_of_materials`, `purchases_stock_in_trade`, `change_in_inventories`, `employee_expense`, `finance_cost`, `depreciation_amortisation`, `other_expenses`, `total_expenses`, `exceptional_items`, `profit_before_tax`, `tax_expense`, `profit_after_tax`, `profit_attributable_owners`, `other_comprehensive_income`, `eps_basic`, `eps_diluted`.
Balance sheet — `ppe`, `cwip`, `intangible_assets`, `goodwill`, `non_current_investments`, `inventories`, `trade_receivables`, `cash_and_equivalents`, `other_bank_balances`, `current_investments`, `total_current_assets`, `total_assets`, `equity_share_capital`, `other_equity`, `total_equity`, `non_controlling_interest`, `long_term_borrowings`, `short_term_borrowings`, `lease_liabilities`, `trade_payables`, `total_current_liabilities`, `total_liabilities`, `total_equity_and_liabilities`.
Cash flow — `cfo`, `cfi`, `cff`, `capex_ppe`, `dividends_paid`, `net_change_in_cash`, `closing_cash`.
Operating profit is **not** a concept here — it is a metric (M07), because companies define it differently.

## Files

### `normalize/taxonomy.py`
- **Process:** load + validate `taxonomy.yaml` (pydantic); build lookup indexes: XBRL QName → concept; normalised label → concept; reject duplicate mappings at load time.
- **Output:** `Taxonomy` object.

### `normalize/mapper.py`
- **Input:** `ParsedFact`, `Taxonomy`, entity sector type.
- **Process:** (1) XBRL QName exact match → `mapping_method='standard'`; (2) normalised label exact match → `'label'`; (3) fuzzy label match ≥ `normalize.fuzzy_suggest_min_score` → **review queue only, with the suggestion attached**, not auto-mapped; (4) user-confirmed mappings stored in `review_queue.resolution_json` and applied as `'manual'`.
- **Output:** `MappedFact | Unmapped`.

### `normalize/units.py`
- **Process:** `value × settings.units.multipliers[scale]` for PDF facts; XBRL facts already in rupees; per-share and share counts untouched; all as `Decimal`.
- **Output:** value in canonical unit.

### `normalize/periods.py`
- **Process:** using `core/fiscal.py`, classify each fact's period as `Q`, `H`, `9M`, `FY` or `INSTANT`; reject periods that don't align with the fiscal calendar → review queue.

### `normalize/basis.py`
- **Process:** basis from XBRL meta / page classifier / filing row, in that order; `unknown` → review queue (`ambiguous_basis`).

### `normalize/review_queue.py`
- **Process:** upsert items keyed by (item_type, isin, concept_raw or label); increment `occurrences`; `resolve(item_id, resolution)` writes the mapping back so next run auto-applies it.
- **Commands:** `finrag review list`, `finrag review resolve <id> --concept revenue`, `finrag review ignore <id>`.

### `normalize/runner.py`
- **Process:** for each `parsed` filing: map → convert → classify period → basis → pass `CanonicalCandidate` list to `facts.writer` (M05 interface; stub in tests) → set filing status `normalized`.
- **Output:** stats: mapped (by method), unmapped, review items.

## Tests and acceptance criteria
1. Fixture fact with a known XBRL QName in taxonomy → mapped `standard`.
2. Label "Revenue from Operations" (case/spacing variants) → mapped `label`.
3. Unknown label → review item; running again increments `occurrences`, no duplicate item.
4. Resolving a review item → next run maps it `manual`.
5. 1,234.56 with scale crore → `12345600000` exactly (Decimal).
6. Duplicate QName in taxonomy → load fails with message.
7. Mapping coverage report: `finrag normalize report --isin X` shows % mapped by statement.

## Out of scope
Storing facts, restatement logic, derivation (M05).
