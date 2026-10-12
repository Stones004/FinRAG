"""Classify a fact's dates as a fiscal period (Q, H, 9M, FY or INSTANT)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from finrag.core.fiscal import period_to_label
from finrag.core.types import Period, PeriodType

_DURATION_TYPES = (PeriodType.Q, PeriodType.H, PeriodType.NINE_M, PeriodType.FY)

ANCHOR_END = "end"
ANCHOR_DAY_BEFORE_START = "day_before_start"

REASON_PERIOD_MISSING = "period_missing"
REASON_PERIOD_MISALIGNED = "period_misaligned"
REASON_PERIOD_KIND = "period_kind_mismatch"


@dataclass(frozen=True)
class PeriodResult:
    """A classified period, or the reason it cannot be."""

    period: Period | None
    reason: str | None = None
    detail: str = ""


def classify(
    start: date | None, end: date | None, kind: str, instant_anchor: str | None = None
) -> PeriodResult:
    """Fact dates + the concept's period_kind -> Period (fiscal-aligned) or a reason.

    A PDF cash-flow table gives opening and closing cash under a duration column: with an
    `instant_anchor` ("end" or "day_before_start") the instant is taken from that column.
    """
    if end is None:
        return PeriodResult(None, REASON_PERIOD_MISSING, "the fact has no period end")
    is_instant = start is None or start == end
    if kind == "instant":
        if not is_instant and start is not None and instant_anchor:
            day = end if instant_anchor == ANCHOR_END else start - timedelta(days=1)
            return PeriodResult(Period(day, day, PeriodType.INSTANT))
        if not is_instant:
            return PeriodResult(None, REASON_PERIOD_KIND, f"{start}..{end} is a duration")
        return PeriodResult(Period(end, end, PeriodType.INSTANT))
    if is_instant:
        return PeriodResult(None, REASON_PERIOD_KIND, f"{end} is an instant")
    assert start is not None
    if start > end:
        return PeriodResult(None, REASON_PERIOD_MISALIGNED, f"{start}..{end} ends before it starts")
    for period_type in _DURATION_TYPES:
        candidate = Period(start, end, period_type)
        try:
            period_to_label(candidate)
        except ValueError:
            continue
        return PeriodResult(candidate)
    return PeriodResult(None, REASON_PERIOD_MISALIGNED, f"{start}..{end} is not a fiscal period")
