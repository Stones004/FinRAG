# M05 — Fact Store, Restatements and Derivations

**Package:** `src/finrag/facts/` · **Depends on:** M04 · **Tests:** `tests/m05_facts/`

## Purpose
Write canonical facts append-only, detect restatements, derive discrete periods from cumulative ones where needed, and provide the "current view" every later module reads.

## Files

### `facts/writer.py`
- **Input:** `list[CanonicalCandidate]` from M04.
- **Process:** for each candidate, look for an existing fact with the same (isin, concept, period_start, period_end, basis):
  - none → insert;
  - same value and same source filing → skip (idempotent rerun);
  - same value, different filing → insert (records that a later filing confirmed it);
  - different value → insert and call `restatement.record()`.
- **Output:** written fact IDs; stats (inserted, skipped, restated).
- **Rule:** never UPDATE or DELETE `canonical_facts`.

### `facts/restatement.py`
- **Process:** compute abs and relative difference vs the most recent prior fact; `flagged = 1` only if both exceed `settings.restatement.relative_tolerance` and `absolute_tolerance_inr`.
- **Output:** `restatements` row.

### `facts/current_view.py`
- **Input:** isin, concept(s), period(s), basis.
- **Process:** latest fact by `filed_on`, then `revision_no` of its filing, then `created_at` (an original and its revision share the board-meeting date that XBRL gives as `filed_on`, so `revision_no` decides); filings with status `quarantined` are never a source; option `as_of=<date>` returns what was known on that date.
- **Output:** `get_fact(...) -> Fact | None`, `get_facts(...) -> list[Fact]`, `get_series(isin, concept, period_type, basis, n)`.
- **Rule:** every later module reads facts only through this file.

### `facts/derivation.py`
- **Input:** current view for an ISIN; `core/fiscal.components_of`.
- **Process:** for duration concepts only, derive a missing discrete period when all required inputs exist:
  `Q2 = H1 − Q1`, `Q3 = 9M − H1`, `Q4 = FY − 9M`, `H2 = FY − H1`.
  Never derive instants. Never derive if any input is missing or blocked. Record `is_derived=1` and `derivation_json = {formula, input_fact_ids}`.
- **Output:** derived facts via `writer` (so they are also append-only).

### `facts/dependencies.py`
- **Process:** given a newly written or restated fact, return the derived facts and periods that depend on it (via `derivation_json`) so they are re-derived; also return affected (isin, period) pairs for M06/M07 recomputation.
- **Output:** `affected(fact_ids) -> AffectedSet`.

### `facts/cli.py`
- `finrag facts show --isin X --concept revenue [--basis consolidated]` (series with source filing and derived flag)
- `finrag facts restatements --isin X`

## Tests and acceptance criteria
1. Writing the same candidates twice → no new rows.
2. A later filing with a different value → two rows; current view returns the later one; `as_of` before the later filing returns the earlier one.
3. Restatement of 0.3% → recorded, not flagged; 2% and > ₹1 crore → flagged (tolerances from config).
4. H1 = 400, Q1 = 150, no Q2 reported → derived Q2 = 250, marked derived with input IDs.
5. Q2 reported directly → no derivation.
6. Q1 missing → Q2 not derived, and no error.
7. Restating Q1 → `dependencies.affected` returns the derived Q2, which is re-derived with the new value.
8. Instant concepts (balance sheet) are never derived.

## Out of scope
Validation and metrics.
