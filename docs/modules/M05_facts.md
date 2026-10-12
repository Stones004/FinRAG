# M05 — Fact Store, Restatements and Derivations

**Package:** `src/finrag/facts/` · **Depends on:** M04 · **Tests:** `tests/m05_facts/`

## Purpose
Write canonical facts append-only, detect restatements, derive discrete periods from cumulative ones where needed, and provide the "current view" every later module reads.

## Schema change (v4)
`canonical_facts.source_class TEXT NOT NULL` ∈ `xbrl | pdf | ocr | derived` (from the candidate's `extractor`; `derived` for M05's own facts). Derived facts record their inputs' classes in `derivation_json`.

## Files

### `facts/models.py`
- `Fact` (frozen): a `canonical_facts` row plus the source filing's current `filed_on`, `revision_no` and the parsed fact's `confidence`. Properties: `rank` (source precedence: 0 XBRL, 1 derived from XBRL inputs only, 2 PDF/OCR, 3 other derived), `chain` (xbrl | pdf | derived), `order` (filing date as a date, `revision_no`, `created_at`).

### `facts/writer.py`
- **Input:** `list[CanonicalCandidate]` from M04. Entry point `write_candidates(candidates)`, which `finrag normalize run` calls (it then derives for every ISIN touched).
- **Process:** a candidate of a `quarantined` filing is refused (counted). Otherwise look for existing versions with the same (isin, concept, period_start, period_end, basis):
  - same value and same source filing (derived: same input fact IDs) → skip (idempotent rerun);
  - otherwise insert, then judge the new fact against its neighbours **in current-view order within its chain** (xbrl, pdf/ocr, derived): a neighbour with a different value gives a `restatement.record()`. Write order does not matter: a revision written before its original still restates original → revision. XBRL and PDF/OCR facts are never compared with each other.
  - same value, different filing → inserted without a restatement (records that a later filing confirmed it).
- **Output:** `WriteStats` (inserted, skipped, restated, flagged, refused, fact IDs, ISINs).
- **Rule:** never UPDATE or DELETE `canonical_facts`.

### `facts/restatement.py`
- `assess(old, new)` / `record(conn, old, new)`: abs and relative difference; `flagged = 1` only if both exceed `settings.restatement.relative_tolerance` and `absolute_tolerance_inr`. The absolute tolerance is in rupees, so it applies to INR concepts only; per-share and ratio concepts are flagged on the relative tolerance alone. `rel_diff` is NULL when the old value is 0 (then only the absolute test applies). A pair is recorded once.

### `facts/current_view.py`
- `get_fact(isin, concept, period, basis, as_of=None)`, `get_facts(isin, concepts, periods, basis, as_of)`, `get_series(isin, concept, period_type, basis, n, as_of)`, `history(...)` (every stored version).
- **Process:** among the versions of a key, the best `rank` wins, then the latest by filing date (dates only, as sources differ in granularity), `revision_no`, `created_at`. The filing's *current* `filed_on` is used, so a `set-filed-on` correction takes effect at once. Filings with status `quarantined` are never a source. `as_of=<date>` returns what was known on that date (filing date, or the storage date when the filing has none).
- **Source precedence:** an XBRL fact (or a fact derived from XBRL inputs only) beats a PDF/OCR fact for the same key, whichever is later or has a higher `confidence`; reported beats derived within a class. PDF/OCR facts only fill gaps; all rows stay (append-only). A PDF/OCR value that differs from XBRL is a **cross-source check in M06**, not a restatement and not M05's job.
- **Rule:** every later module reads facts only through this file.

### `facts/derivation.py`
- **Process:** for each (isin, concept, basis) whose concept `can_derive` (duration; not `derivable: false`; unit INR unless `additive: true`), derive a missing Q2, Q3, Q4 or H2 using `core.fiscal.cumulative_steps`, taking the first identity whose inputs all exist: `Q2 = H1 − Q1`, `Q3 = 9M − H1`, `Q4 = FY − 9M`, `H2 = FY − H1`.
  - Inputs must be **reported** (never derived) current facts, XBRL or PDF/OCR at or above `parsing.pdf.min_table_confidence`.
  - A target that is reported directly (any source) is never derived. Instants and non-derivable concepts are never derived. A missing input means no derivation and no error.
  - "Blocked" periods are ignored here; M06/M07 gate them.
  - Derived fact: `source_filing_id`/`filed_on` of the **latest input's filing**, `is_derived = 1`, `source_class = derived`, `derivation_json = {formula, input_fact_ids, inputs[{fact_id, sign, period, source_class}], input_classes, all_xbrl}`.
- **Output:** derived facts through the same append path (a changed re-derivation is a restatement in the derived chain).

### `facts/dependencies.py`
- `affected(fact_ids) -> AffectedSet(derived_fact_ids, periods, rederive)`: derived facts whose `input_fact_ids` include any version of the same key as a given fact, the (isin, period, basis) pairs M06/M07 must recompute (the facts' periods, their dependants and the periods now derivable), and the groups to re-derive (`derive_groups(affected.rederive)`).

### `facts/cli.py`
- `finrag facts show --isin X --concept revenue [--basis consolidated]`, `finrag facts restatements --isin X [--flagged-only]`, `finrag facts derive --isin X`.

## Tests and acceptance criteria
1. Writing the same candidates twice → no new rows.
1a. An XBRL fact and a later PDF/OCR fact for the same key: current view returns the XBRL fact; with no XBRL fact the PDF/OCR fact is returned; no restatement between them.
2. A later filing with a different value → two rows; current view returns the later one; `as_of` before the later filing returns the earlier one.
3. Restatement of 0.3% → recorded, not flagged; 2% and > ₹1 crore → flagged (tolerances from config).
4. H1 = 400, Q1 = 150, no Q2 reported → derived Q2 = 250, marked derived with input IDs.
5. Q2 reported directly → no derivation.
6. Q1 missing → Q2 not derived, and no error.
7. Restating Q1 → `dependencies.affected` returns the derived Q2, which is re-derived with the new value.
8. Instant concepts (balance sheet) are never derived.
9. Real data (`pytest -m golden`, read-only): Gabriel FY26 revision is current for `cfo` (3,452.74 mn) with the original (4,435.59 mn) flagged and `as_of` 2026-06-01 returning the original; unchanged P&L facts have no restatement; Netweb's quarantined originals contribute no facts; XBRL beats the Netweb Q4 FY26 PDF; H2 FY26 is derived where H1 and FY exist; reported quarters and non-derivable concepts (EPS, ratios, bank NPA/CET1) are never derived.

## Out of scope
Validation (including the XBRL vs PDF/OCR `cross_source` check) and metrics.
