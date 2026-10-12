"""Securities master: NSE/BSE listing files merged on ISIN, with lookups."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from finrag.core.config import ConfigError, Settings, get_settings
from finrag.core.logging import get_logger

# Real column names of NSE's EQUITY_L.csv (checked 2026-10-11, header whitespace stripped).
NSE_COLUMNS = {"isin": "ISIN NUMBER", "name": "NAME OF COMPANY", "symbol": "SYMBOL"}
# TODO: unverified. Fill from the real BSE file once the user provides one (BSE codes can also
# come from the XBRL ScripCode element in M02). Do not rely on these names until then.
BSE_COLUMNS = {"isin": "ISIN No", "name": "Security Name", "code": "Security Code"}

# Words dropped when normalising company names, per the M01 spec.
NAME_NOISE_WORDS = frozenset({"LTD", "LIMITED"})
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")

log = get_logger("watchlist")


def normalize_name(name: str) -> str:
    """Company name -> upper-case, punctuation-free, without LTD/LIMITED."""
    words = _NON_ALNUM.sub(" ", name.upper()).split()
    return " ".join(word for word in words if word not in NAME_NOISE_WORDS)


@dataclass(frozen=True)
class SecurityRecord:
    """One listed company, merged across exchanges."""

    isin: str
    name: str
    nse_symbol: str | None = None
    bse_code: str | None = None

    @property
    def norm_name(self) -> str:
        """Normalised name used for matching."""
        return normalize_name(self.name)


def _merge(first: SecurityRecord, second: SecurityRecord) -> SecurityRecord:
    """Two rows with one ISIN -> one record; the first name wins, missing fields are filled."""
    return replace(
        first,
        nse_symbol=first.nse_symbol or second.nse_symbol,
        bse_code=first.bse_code or second.bse_code,
    )


class SecuritiesMaster:
    """Merged securities records with lookups by ISIN, symbol, BSE code and normalised name."""

    def __init__(self, records: list[SecurityRecord]) -> None:
        self._by_isin: dict[str, SecurityRecord] = {}
        for record in records:
            existing = self._by_isin.get(record.isin)
            self._by_isin[record.isin] = _merge(existing, record) if existing else record
        self._by_symbol = {r.nse_symbol.upper(): r for r in self._by_isin.values() if r.nse_symbol}
        self._by_code = {r.bse_code: r for r in self._by_isin.values() if r.bse_code}
        by_name: dict[str, list[SecurityRecord]] = defaultdict(list)
        for record in self._by_isin.values():
            by_name[record.norm_name].append(record)
        self._by_name = dict(by_name)

    def __len__(self) -> int:
        return len(self._by_isin)

    def records(self) -> list[SecurityRecord]:
        """All records."""
        return list(self._by_isin.values())

    def by_isin(self, isin: str) -> SecurityRecord | None:
        """ISIN -> record or None."""
        return self._by_isin.get(isin.strip().upper())

    def by_symbol(self, symbol: str) -> SecurityRecord | None:
        """NSE symbol (case-insensitive) -> record or None."""
        return self._by_symbol.get(symbol.strip().upper())

    def by_code(self, code: str) -> SecurityRecord | None:
        """BSE code -> record or None."""
        return self._by_code.get(code.strip())

    def by_norm_name(self, name: str) -> list[SecurityRecord]:
        """Name (normalised here) -> every record with that normalised name."""
        return list(self._by_name.get(normalize_name(name), []))

    def norm_names(self) -> list[str]:
        """All distinct normalised names (for fuzzy matching)."""
        return list(self._by_name)


def _read_rows(path: Path, columns: dict[str, str]) -> list[dict[str, str]]:
    """CSV -> rows keyed by canonical field; header names and values are whitespace-stripped."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        reader.fieldnames = [(name or "").strip() for name in reader.fieldnames or []]
        missing = [col for col in columns.values() if col not in reader.fieldnames]
        if missing:
            raise ConfigError(f"{path.name}: missing expected column(s) {missing}")
        rows = []
        for raw in reader:
            row = {key: (raw.get(col) or "").strip() for key, col in columns.items()}
            if row["isin"] and row["name"]:
                rows.append({**row, "isin": row["isin"].upper()})
        return rows


def load_nse(path: Path) -> list[SecurityRecord]:
    """NSE equity list CSV -> records."""
    return [
        SecurityRecord(isin=r["isin"], name=r["name"], nse_symbol=r["symbol"] or None)
        for r in _read_rows(path, NSE_COLUMNS)
    ]


def load_bse(path: Path) -> list[SecurityRecord]:
    """BSE equity list CSV -> records (column names unverified; see BSE_COLUMNS)."""
    return [
        SecurityRecord(isin=r["isin"], name=r["name"], bse_code=r["code"] or None)
        for r in _read_rows(path, BSE_COLUMNS)
    ]


def load_master(settings: Settings | None = None) -> SecuritiesMaster:
    """Load the NSE and BSE files named in settings; a missing BSE file only logs a warning."""
    settings = settings or get_settings()
    folder = settings.paths.reference
    nse_path = folder / settings.sources.securities_master.nse_file
    bse_path = folder / settings.sources.securities_master.bse_file
    if not nse_path.is_file():
        raise ConfigError(f"NSE securities master not found: {nse_path}")
    records = load_nse(nse_path)
    if bse_path.is_file():
        records += load_bse(bse_path)
    else:
        log.warning("BSE securities master not found: %s", bse_path, extra={"event": "no_bse"})
    return SecuritiesMaster(records)
