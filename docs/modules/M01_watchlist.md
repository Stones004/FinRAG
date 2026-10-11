# M01 — Watch List and Entity Registry

**Package:** `src/finrag/watchlist/` · **Depends on:** M00 · **Tests:** `tests/m01_watchlist/`

## Purpose
Turn the user's watch list (names, symbols or codes) into stable entities keyed by ISIN, with aliases, sector type and status. Adding a company = editing `config/watchlist.yaml` and running one command.

## Inputs
**`config/watchlist.yaml`** (user-edited):
```yaml
companies:
  - id: EXAMPLE            # NSE symbol, BSE code, ISIN or name
  - id: "500999"
    sector_type: bank      # optional; default from settings.watchlist.default_sector_type
    basis: standalone      # optional; default from settings.defaults.basis
  - id: INE000X01010
    status: paused         # active | paused
```
**Securities master** files in `settings.paths.reference` (downloaded once by the user from the exchanges; exact column names must be read from the real files — do not assume).

## Files

### `watchlist/schema.py`
- **Does:** pydantic models for the watch list YAML and for an `Entity` record; validates `sector_type` against `settings.watchlist.allowed_sector_types`.

### `watchlist/master.py`
- **Input:** securities master CSVs.
- **Process:** load each exchange file through a small column-mapping dict defined in this file's loader (map real column names → `isin, name, symbol/code`); normalise names (upper-case, strip "LTD", "LIMITED", punctuation); merge NSE and BSE rows on ISIN.
- **Output:** `SecuritiesMaster` with lookups by ISIN, symbol, code, normalised name.
- **TODO for Claude Code:** column names are filled in once the user provides the real files. Until then, tests use a synthetic master fixture.

### `watchlist/resolver.py`
- **Input:** one identifier string, `SecuritiesMaster`.
- **Process:** in order — ISIN regex exact; NSE symbol exact; BSE code exact; normalised name exact; fuzzy name (`rapidfuzz`, ≥ `settings.watchlist.name_match_min_score`).
- **Output:** `Resolution(status = resolved | ambiguous | not_found, isin, candidates, method)`.
- **Rule:** never pick between two candidates automatically.

### `watchlist/registry.py`
- **Input:** resolutions + YAML overrides.
- **Process:** upsert `entities`; write `entity_aliases` (symbol, code, name, user input); ambiguous/not_found → `review_queue` (`entity_ambiguous`); detect name/symbol changes vs existing rows and record `corporate_events` (rename) with today's date.
- **Output:** `sync_watchlist() -> SyncReport(added, updated, unchanged, needs_review)`; `newly_added` ISINs are returned so M02 can backfill them.

### `watchlist/cli.py`
- **Commands:** `finrag watchlist sync`, `finrag watchlist list`, `finrag watchlist resolve "<text>"` (dry-run).

## Tests and acceptance criteria
1. Symbol, code, ISIN and exact name each resolve to the same ISIN.
2. A name matching two companies → `ambiguous`, review-queue item created, entity not added.
3. Running `sync` twice → second run reports 0 added, 0 updated.
4. Removing a company from YAML does **not** delete it; it is marked `paused` (history is kept).
5. Symbol change in the master → alias added with `valid_to` on the old one, rename event recorded.
6. Invalid `sector_type` fails validation with a clear message.

## Out of scope
Downloading the master files automatically (later, via M02 adapters if terms allow).
