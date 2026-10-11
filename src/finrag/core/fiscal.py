"""Fiscal-period logic: date <-> label conversion, shifting, components and identities.

Assumptions (recorded in docs/PROGRESS.md): a fiscal year is labelled by the calendar year in
which it ends, for any fy_start_month; two-digit labels are read as 20yy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache

from finrag.core.config import FiscalSettings, get_settings
from finrag.core.types import Period, PeriodType

_MONTHS = {PeriodType.Q: 3, PeriodType.H: 6, PeriodType.NINE_M: 9, PeriodType.FY: 12}
_CENTURY = 2000
_QUARTER_MONTHS = 3
_HALF_MONTHS = 6


@dataclass(frozen=True)
class Term:
    """One signed term of an identity."""

    sign: int
    period: Period


@dataclass(frozen=True)
class Identity:
    """target = sum(sign * period) over terms."""

    target: Period
    terms: tuple[Term, ...]


def _fiscal(fiscal: FiscalSettings | None) -> FiscalSettings:
    """Return the given fiscal settings or the configured ones."""
    return fiscal or get_settings().fiscal


def _add_months(d: date, months: int) -> date:
    """First-of-month date + whole months -> first-of-month date."""
    index = d.year * 12 + (d.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def _end_for(start: date, months: int) -> date:
    """First-of-month start + length in months -> last day of the period."""
    return _add_months(start, months) - timedelta(days=1)


def _fy_start(year: int, fiscal: FiscalSettings) -> date:
    """Fiscal year (ending-year number) -> its first day."""
    month = fiscal.fy_start_month
    return date(year, 1, 1) if month == 1 else date(year - 1, month, 1)


def fiscal_year_of(d: date, fiscal: FiscalSettings | None = None) -> int:
    """Date -> fiscal year number (the calendar year in which that fiscal year ends)."""
    month = _fiscal(fiscal).fy_start_month
    return d.year if month == 1 or d.month < month else d.year + 1


def fy_label(year: int, fiscal: FiscalSettings | None = None) -> str:
    """Fiscal year number -> label such as 'FY26' (per fy_label_format)."""
    return _fiscal(fiscal).fy_label_format.replace("{yy}", f"{year % 100:02d}")


@cache
def _label_regex(fmt: str) -> re.Pattern[str]:
    """fy_label_format -> compiled regex for '[Q1|H1|9M][ ]<fy label>' (space optional)."""
    fy_pattern = re.escape(fmt).replace(re.escape("{yy}"), r"(?P<yy>\d{2})")
    return re.compile(rf"^\s*(?:(?P<kind>Q[1-4]|H[12]|9M)\s*)?{fy_pattern}\s*$", re.IGNORECASE)


def _build(year: int, ptype: PeriodType, offset: int, fiscal: FiscalSettings) -> Period:
    """Fiscal year, period type and month offset from FY start -> Period."""
    start = _add_months(_fy_start(year, fiscal), offset)
    return Period(start, _end_for(start, _MONTHS[ptype]), ptype)


def label_to_period(label: str, fiscal: FiscalSettings | None = None) -> Period:
    """'Q1 FY26' | 'H1 FY26' | '9M FY26' | 'FY26' -> Period."""
    fiscal = _fiscal(fiscal)
    match = _label_regex(fiscal.fy_label_format).match(label)
    if not match:
        raise ValueError(f"unrecognised period label: {label!r}")
    year = _CENTURY + int(match.group("yy"))
    kind = (match.group("kind") or "FY").upper()
    if kind == "FY":
        return _build(year, PeriodType.FY, 0, fiscal)
    if kind == "9M":
        return _build(year, PeriodType.NINE_M, 0, fiscal)
    number = int(kind[1])
    if kind[0] == "Q":
        return _build(year, PeriodType.Q, _QUARTER_MONTHS * (number - 1), fiscal)
    return _build(year, PeriodType.H, _HALF_MONTHS * (number - 1), fiscal)


def _locate(period: Period, fiscal: FiscalSettings) -> tuple[int, int]:
    """Period -> (fiscal year, month offset from FY start); raises if not fiscal-aligned."""
    if period.type is PeriodType.INSTANT:
        raise ValueError("INSTANT periods have no fiscal label")
    months = _MONTHS[period.type]
    year = fiscal_year_of(period.start, fiscal)
    fy_start = _fy_start(year, fiscal)
    offset = (period.start.year - fy_start.year) * 12 + period.start.month - fy_start.month
    step = months if months < 9 else 12  # 9M and FY only start at offset 0
    aligned = (
        period.start.day == 1
        and period.end == _end_for(period.start, months)
        and offset % step == 0
        and offset + months <= 12
    )
    if not aligned:
        raise ValueError(f"{period} is not aligned to the fiscal calendar")
    return year, offset


def period_to_label(period: Period, fiscal: FiscalSettings | None = None) -> str:
    """Period -> 'Q1 FY26' | 'H1 FY26' | '9M FY26' | 'FY26'."""
    fiscal = _fiscal(fiscal)
    year, offset = _locate(period, fiscal)
    fy = fy_label(year, fiscal)
    if period.type is PeriodType.FY:
        return fy
    if period.type is PeriodType.NINE_M:
        return f"9M {fy}"
    if period.type is PeriodType.Q:
        return f"Q{offset // _QUARTER_MONTHS + 1} {fy}"
    return f"H{offset // _HALF_MONTHS + 1} {fy}"


def is_cumulative(period: Period) -> bool:
    """True for H, 9M and FY periods."""
    return period.type in (PeriodType.H, PeriodType.NINE_M, PeriodType.FY)


def shift_period(
    period: Period, years: int = 0, periods: int = 0, fiscal: FiscalSettings | None = None
) -> Period:
    """Move a period by whole years and/or whole periods of its own length (same type)."""
    fiscal = _fiscal(fiscal)
    _locate(period, fiscal)  # validates alignment and rejects INSTANT
    months = _MONTHS[period.type]
    start = _add_months(period.start, years * 12 + periods * months)
    return Period(start, _end_for(start, months), period.type)


def components_of(period: Period, fiscal: FiscalSettings | None = None) -> list[Period]:
    """Cumulative period -> its discrete quarters (FY -> Q1..Q4); [] for a quarter."""
    fiscal = _fiscal(fiscal)
    year, offset = _locate(period, fiscal)
    if period.type in (PeriodType.Q, PeriodType.INSTANT):
        return []
    count = _MONTHS[period.type] // _QUARTER_MONTHS
    return [_build(year, PeriodType.Q, offset + _QUARTER_MONTHS * i, fiscal) for i in range(count)]


def cumulative_steps(period: Period, fiscal: FiscalSettings | None = None) -> list[Identity]:
    """Period -> one-step identities that yield it from other periods (for derivation)."""
    fiscal = _fiscal(fiscal)
    year, offset = _locate(period, fiscal)

    def p(ptype: PeriodType, off: int) -> Period:
        return _build(year, ptype, off, fiscal)

    q1, q2, q3, q4 = (p(PeriodType.Q, _QUARTER_MONTHS * i) for i in range(4))
    h1, h2 = p(PeriodType.H, 0), p(PeriodType.H, _HALF_MONTHS)
    nine, full = p(PeriodType.NINE_M, 0), p(PeriodType.FY, 0)

    def identity(*routes: tuple[tuple[int, Period], ...]) -> list[Identity]:
        """Routes -> Identity list; H2 is never filed directly, so routes using it rank later."""
        found = [Identity(period, tuple(Term(sign, term) for sign, term in r)) for r in routes]
        return sorted(found, key=lambda i: sum(t.period == h2 for t in i.terms))

    if period == full:
        return identity(((1, nine), (1, q4)), ((1, h1), (1, h2)))
    if period == nine:
        return identity(((1, h1), (1, q3)))
    if period == h1:
        return identity(((1, q1), (1, q2)))
    if period == h2:
        return identity(((1, full), (-1, h1)))
    if period == q2:
        return identity(((1, h1), (-1, q1)))
    if period == q3:
        return identity(((1, nine), (-1, h1)))
    if period == q4:
        return identity(((1, full), (-1, nine)), ((1, h2), (-1, q3)))
    return []
