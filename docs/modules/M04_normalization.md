# M04 — Normalization and Taxonomy

**Package:** `src/finrag/normalize/` · **Depends on:** M03 · **Tests:** `tests/m04_normalize/`

## Purpose
Map every parsed fact to a canonical concept, convert to rupees, assign period type and basis, and hand clean candidates to M05. Anything that can't be mapped goes to the review queue — never dropped. Facts that are understood but deliberately not mapped are counted by reason, stay in `parsed_facts` (later modules, e.g. the signals work, read them) and are never queued.

## Inputs
- `parsed_facts` for filings with status `parsed`.
- `config/taxonomy.yaml` (owned by this module).

### `config/taxonomy.yaml` structure
```yaml
version: 1
families: [indas, banking]            # = filings.taxonomy_family values that have mappings
xbrl_units:                           # unit_raw strings accepted per canonical unit
  INR: ["iso4217:INR"]
  INR_PER_SHARE: ["iso4217:INR/xbrli:shares"]
  SHARES: ["xbrli:shares"]
  RATIO: ["xbrli:pure"]
unmapped_reasons:                     # reason -> description; `duplicate_of:<concept>` is also allowed
  cash_flow_adjustment: ...
concepts:
  revenue:
    label: Revenue from operations
    description: ""                   # optional notes (e.g. what a tag includes)
    statement: profit_and_loss        # profit_and_loss | balance_sheet | cash_flow | other
    period_kind: duration             # duration | instant
    unit: INR                         # INR | INR_PER_SHARE | SHARES | RATIO
    sign: as_reported                 # only as_reported: values are never flipped
    sign_convention: natural          # natural | outflow_positive (read by M07 formulas)
    sectors: [general, nbfc]          # omit = all
    xbrl_concepts:                    # per taxonomy family; QNames seen in real filings only
      indas: ["in-capmkt:RevenueFromOperations"]
    labels: ["Revenue from operations"]   # statement labels seen in real PDFs
    pdf_sign: as_printed              # as_printed | negate (PDF/OCR values -> XBRL sign)
    zero_is_placeholder: false        # true: a reported 0 means "not disclosed" (not stored)
    derivable: true                   # false: M05 never derives it from cumulative periods
    opening_concept: opening_cash     # only closing_cash: see "Opening cash"
known_unmapped:                       # per family: QName -> reason | {reason, note}
  banking:
    "in-capmkt:SegmentRevenue": { reason: segment, note: "never revenue" }
```
**Lookups are keyed by (taxonomy family, QName)**, the family coming from `filings.taxonomy_family` (`indas` or `banking`; a filing whose family has no mappings is skipped with an error, never mapped by guess). The same QName may appear in both families (`Income`, `Assets`, `TaxExpense`...) and map to the same concept; a QName may not map to two concepts within a family, and may not be both in `concepts` and `known_unmapped`.

**Rules for filling it (MASTER_BUILD §3.6):** every QName must have been seen in a real filing; every PDF label must have been seen in a real parsed PDF (the current list comes from the Netweb Q4 FY26 results scan, each checked by value against the XBRL revision). A concept with no tag in any real filing keeps `xbrl_concepts` empty with a marked TODO (`lease_liabilities`). Banks have their own concepts (`bank_*`, `sectors: [bank]`) where no company concept has the same meaning.

**Concepts:** the starter list, with these changes decided at review: trade receivables and payables are split (`trade_receivables_current`, `trade_receivables_noncurrent`, `trade_payables_current`, `trade_payables_noncurrent`; M07 sums them where a metric needs the total, e.g. DSO); `capex_intangibles`, `opening_cash` added; banks: `bank_operating_profit_before_provisions`, `bank_gross_npa`, `bank_net_npa`, `bank_gross_npa_ratio`, `bank_net_npa_ratio`, `bank_cet1_ratio` (unit `RATIO` = fractions, 0.0117 for 1.17 %); bank equity is `bank_share_capital + bank_reserves_and_surplus`, a helper metric in M07, not a concept; `short_term_borrowings` is the `BorrowingsCurrent` tag and includes current maturities of long-term debt (noted in its description). Operating profit is **not** a concept — it is a metric (M07), because companies define it differently.

**`known_unmapped`** lists every other entity-total QName seen in the real files, each with a reason from `unmapped_reasons` (`cash_flow_adjustment`, `cash_flow_item`, `income_statement_item`, `balance_sheet_item`, `segment`, `related_party`, `notes_disclosure`, `share_data`, `bank_disclosure`, `ratio`, `metadata`) or `duplicate_of:<concept>`. These facts are counted per reason (jobs row and `finrag normalize report`) and never queued. Promote one to `concepts` when a metric needs it. A QName in neither `concepts` nor `known_unmapped` is the only thing that goes to the review queue as `unmapped_concept`. Bank `SegmentRevenue`/`SegmentRevenueFromOperations` are `segment` with a note that they include inter-segment revenue and are never revenue.

**Duplicates:** `OtherComprehensiveIncomeNetOfTaxes` is `duplicate_of:other_comprehensive_income`. For any `duplicate_of` QName, if the same filing carries the target with a different value for the same period and basis, a `normalization_issue` (`duplicate_mismatch`) item is raised and neither value is silently preferred.

**Opening cash:** `CashAndCashEquivalentsCashFlowStatement` carries both balances. `closing_cash` has `opening_concept: opening_cash`: an instant dated before the start of the filing's cash-flow period (the earliest start of its cash-flow duration facts, per basis) is `opening_cash` (the prior period end), the rest is `closing_cash`. A PDF cash-flow table gives both under a duration column: closing is taken at the column end, opening at the day before the column start. M06 adds the rule `opening_cash` = prior `closing_cash`.

**Signs:** the XBRL convention is the canonical one and XBRL values are stored as reported. `sign_convention: outflow_positive` (capex, dividends paid) tells M07 the XBRL value is a positive outflow. The Netweb results PDF prints the same outflows as negatives, so each concept has `pdf_sign: as_printed | negate` (default `as_printed`; `negate` on `capex_ppe`, `capex_intangibles`, `dividends_paid`): M04 converts PDF/OCR values to the XBRL convention, so both sources carry one sign. Absolute values are never compared: a real sign error (e.g. negative operating cash flow) must stay visible to M05's cross-check and M06. The combined PDF line "Purchase of property, plant and equipment and intangibles" has no label mapping (it is PPE plus intangibles).

**Statement scope of labels:** `parsed_facts.statement_type` (set by M03 from the page classifier) is the statement of the page a PDF/OCR fact was read on. A label maps only to a concept whose `statement` equals it ("Finance costs" on the cash-flow page is not the P&L `finance_cost`); otherwise it is `Unmapped` with the statement in the review item (key = isin, label, statement; a resolution applies to that statement only). A page whose statement type is itself an `unmapped_reasons` entry (`segment`) gives known-unmapped with that reason. Facts without a `statement_type` (XBRL, rows parsed before schema v3) are matched by label alone.

**Placeholder zeros and stock measures:** `zero_is_placeholder: true` marks concepts where a reported 0 means "not disclosed": such a fact is counted (`placeholder_zero`) and not passed on, never stored as a false 0. Real case: HDFC Bank's consolidated filings tag `GrossNonPerformingAssets`, `NonPerformingAssets`, `CET1Ratio`, `PercentageOfGrossNpa` and `PercentageOfNpa` as 0 (no dimensioned or text-block fact holds the real figure); only the standalone filing carries values (gross NPA 358.46 bn, net NPA 123.57 bn, 1.17 % and 0.41 %, CET1 19.57 %). `derivable: false` (NPAs, ratios) tells M05 never to derive them by subtracting cumulative periods.

## Files

### `normalize/taxonomy.py`
- **Process:** load + validate the registry (pydantic, `extra = forbid`); build indexes: (family, QName) → concept; normalised label → concept; known-unmapped (family, QName) → reason; reject duplicate mappings, unknown reasons/families/concepts and malformed QNames at load time, naming every problem. `normalise_label`: lowercase, `&` → `and`, drop enumerators, roman-numeral prefixes (OCR-garbled ones too) and bracketed notes, collapse punctuation/whitespace.
- **Output:** `Taxonomy` object.

### `normalize/mapper.py`
- **Input:** `ParsedFact`, `Taxonomy`, the filing's family, entity sector type.
- **Process:** XBRL facts: (1) (family, QName) in `concepts` → `'standard'`; (2) `known_unmapped` → `Known(reason)`; (3) user resolution → `'manual'`; (4) user-ignored → `Known('user_ignored')`; (5) otherwise `Unmapped` (queued). PDF/OCR facts: (1) normalised label exact match, within the statement of the page → `'label'`; (2) user resolution → `'manual'`; (3) fuzzy label match ≥ `normalize.fuzzy_suggest_min_score` → **review queue only, with the suggestion attached**, never auto-mapped. A mapped concept whose `sectors` exclude the entity's sector is counted `not_applicable`.
- **Output:** `Mapped | NotApplicable | Known | Unmapped`.

### `normalize/units.py`
- **Process:** `value × scale` for PDF facts, then × −1 when the concept has `pdf_sign: negate` (`parsed_facts.scale` is already the integer multiplier into rupees taken from `settings.units.multipliers`, e.g. crore = 10,000,000; NULL means the page had no unit line, so the fact is not converted and goes to the review queue; M03 may have taken the unit from an earlier statement page, see `source_loc`); XBRL facts already in rupees, and their `unit_raw` must be one of `xbrl_units` for the concept's unit; per-share and share counts untouched; all as `Decimal`.
- **Output:** value in canonical unit, or a reason (`unit_missing`, `unit_mismatch`, `nil`).

### `normalize/periods.py`
- **Process:** using `core/fiscal.py`, classify each fact's period as `Q`, `H`, `9M`, `FY` or `INSTANT` (instants have `period_start = period_end`); reject periods that don't align with the fiscal calendar, or that contradict the concept's `period_kind`, → review queue.

### `normalize/basis.py`
- **Process:** basis of the fact (XBRL meta / page classifier, already applied by M03), then the filing row; `unknown` → review queue (`ambiguous_basis`, one item per filing).

### `normalize/review_queue.py`
- **Process:** upsert items keyed by (item_type, isin, concept_raw or label); increment `occurrences`; `resolve(item_id, concept)` writes the mapping to `review_queue.resolution_json` so the next run applies it as `manual` (the mapping applies to every ISIN of that family/label); `ignore` stops the key being queued again.
- **Item types:** `unmapped_concept`, `ambiguous_basis`, and (new) `normalization_issue` with a `reason` in the payload: `unit_missing`, `unit_mismatch`, `period_missing`, `period_misaligned`, `period_kind_mismatch`, `duplicate_mismatch`, `duplicate_candidates` (two candidates with one key and different values: both dropped), `low_confidence_sole_source`, `no_name`.
- **Commands:** `finrag review list [--status open|resolved|ignored|all] [--type T]`, `finrag review resolve <id> --concept revenue` (unmapped_concept only), `finrag review ignore <id>`.

### Source precedence (rule for M04 and M05)
XBRL facts outrank PDF/OCR facts. When an XBRL fact and a `pdf_table`/`ocr` fact exist for the same ISIN, concept, period and basis, the XBRL fact wins: PDF/OCR facts only fill gaps and are never passed on as the value that replaces an XBRL one. M04 keeps `extractor` (and `ocr_backend`, `confidence`) on every `CanonicalCandidate` so M05 can apply this; M06's sum checks are the second safety net for 100x OCR errors. A PDF/OCR candidate with `confidence` below `parsing.pdf.min_table_confidence` whose key no XBRL fact of the ISIN covers is still passed on, with a `low_confidence_sole_source` item.

### `normalize/models.py`
- `CanonicalCandidate` (frozen dataclass): isin, filing_id, parsed_fact_id, concept, period_start, period_end, period_type, basis, value (`Decimal`), unit, mapping_method, extractor, ocr_backend, confidence, filed_on; property `key` = (concept, period_start, period_end, basis).
- `CandidateWriter` (Protocol): `__call__(candidates) -> object`. **M05 provides `facts.writer.write_candidates(candidates)`** matching it; M05 imports `CanonicalCandidate` from here.

### `normalize/runner.py`
- **Process:** for each `parsed` filing (never `quarantined`): skip dimensioned facts (`dimensions_json` not NULL; counted and left in `parsed_facts`), skip nil facts (counted) → map → convert → classify period → basis → queue review items → pass the `CanonicalCandidate` list to the writer → set filing status `normalized`. A failure (unknown family, writer error) leaves the filing `parsed` with the error in the jobs row. One `jobs` row per filing.
- **Output:** stats: considered, candidates, mapped by method and by statement, known-unmapped by reason, not applicable, unmapped, issues by reason.

### `normalize/report.py` and `normalize/cli.py`
- `finrag normalize report --isin X` (read-only, writes nothing): per-statement mapped counts and % of the entity-total facts considered, mapped by method, known-unmapped counts **by reason**, not-applicable, unmapped keys and issues that a run would queue.
- `finrag normalize run [--filing ID]`: needs `facts.writer.write_candidates` (M05); until M05 exists it says so and exits.

## Tests and acceptance criteria
1. Fixture fact with a known XBRL QName in taxonomy → mapped `standard`.
2. Label "Revenue from Operations" (case/spacing variants) → mapped `label`.
3. Unknown label → review item; running again increments `occurrences`, no duplicate item. (A fuzzy hit is only a suggestion.)
4. Resolving a review item → next run maps it `manual`. (An ignored item is not queued again.)
5. 1,234.56 with scale 10,000,000 (crore) → `12345600000` exactly (Decimal).
5a. A PDF/OCR candidate and an XBRL candidate for the same ISIN, concept, period and basis both reach the writer carrying their `extractor`; a PDF/OCR candidate with `confidence` below `parsing.pdf.min_table_confidence` is never a sole source without a review item.
6. Duplicate QName in taxonomy → load fails with message.
7. Mapping coverage report: `finrag normalize report --isin X` shows % mapped by statement and known-unmapped counts by reason.
8. Statement scope and signs: the same label on a P&L page and a cash-flow page gives one `finance_cost` candidate (P&L) and an unmapped item (cash flow); a PDF dividend printed -141.63 mn equals the XBRL +141.63 mn after `pdf_sign: negate`; a reported 0 for a `zero_is_placeholder` concept is counted, not stored.
9. Real data (golden, read-only): every entity-total fact of the real XBRL filings is mapped or known-unmapped (none queued, no issues); every `golden_values.yaml` entry maps to its concept and the verified ones keep their value; the Netweb scan's label-mapped candidates agree with the XBRL revision within rounding; Netweb opening cash FY26 = closing cash FY25; HDFC standalone NPA/CET1 values stored and the consolidated zeros not.

## Out of scope
Storing facts, restatement logic, derivation (M05).
