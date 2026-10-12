"""Resolve one identifier (ISIN, symbol, BSE code or name) to an ISIN."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz, process

from finrag.core.config import Settings, get_settings
from finrag.core.types import is_valid_isin, looks_like_isin
from finrag.watchlist.master import SecuritiesMaster, SecurityRecord, normalize_name

ResolutionStatus = Literal["resolved", "ambiguous", "not_found"]


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving one identifier; `reason` explains ambiguous and not_found."""

    status: ResolutionStatus
    isin: str | None = None
    candidates: tuple[SecurityRecord, ...] = field(default_factory=tuple)
    method: str | None = None
    reason: str = ""


def _resolved(record: SecurityRecord, method: str) -> Resolution:
    return Resolution("resolved", record.isin, (record,), method)


def _from_candidates(records: list[SecurityRecord], method: str, what: str) -> Resolution:
    """Candidates -> resolved if exactly one; ambiguous (never a pick) if several."""
    unique = list({r.isin: r for r in records}.values())
    if len(unique) == 1:
        return _resolved(unique[0], method)
    names = ", ".join(f"{r.name} ({r.isin})" for r in unique)
    return Resolution("ambiguous", None, tuple(unique), method, f"{what} matches {names}")


def resolve(
    identifier: str, master: SecuritiesMaster, settings: Settings | None = None
) -> Resolution:
    """Identifier -> Resolution: ISIN, NSE symbol, BSE code, exact name, then fuzzy name."""
    text = identifier.strip()
    if looks_like_isin(text):
        if not is_valid_isin(text):
            return Resolution(
                "not_found", reason=f"'{text}' looks like an ISIN but its check digit is wrong"
            )
        record = master.by_isin(text)
        if record:
            return _resolved(record, "isin")
        return Resolution(
            "not_found", reason=f"ISIN {text.upper()} is not in the securities master"
        )
    record = master.by_symbol(text)
    if record:
        return _resolved(record, "nse_symbol")
    record = master.by_code(text)
    if record:
        return _resolved(record, "bse_code")
    exact = master.by_norm_name(text)
    if exact:
        return _from_candidates(exact, "name_exact", f"name '{text}'")
    return _fuzzy(text, master, settings or get_settings())


def _fuzzy(text: str, master: SecuritiesMaster, settings: Settings) -> Resolution:
    """Fuzzy name match at or above settings.watchlist.name_match_min_score."""
    query = normalize_name(text)
    if not query:
        return Resolution("not_found", reason="identifier is empty after normalisation")
    hits = process.extract(
        query,
        master.norm_names(),
        scorer=fuzz.token_sort_ratio,
        score_cutoff=settings.watchlist.name_match_min_score,
        limit=None,
    )
    records = [r for name, _score, _idx in hits for r in master.by_norm_name(name)]
    if not records:
        return Resolution("not_found", reason=f"no match for '{text}' in the securities master")
    return _from_candidates(records, "name_fuzzy", f"name '{text}'")
