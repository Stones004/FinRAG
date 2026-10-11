"""Shared enums, the Period type and Decimal money helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class Basis(StrEnum):
    CONSOLIDATED = "consolidated"
    STANDALONE = "standalone"
    UNKNOWN = "unknown"


class PeriodType(StrEnum):
    Q = "Q"
    H = "H"
    NINE_M = "9M"
    FY = "FY"
    INSTANT = "INSTANT"


class FilingFormat(StrEnum):
    XBRL = "xbrl"
    PDF = "pdf"


class FilingStatus(StrEnum):
    FETCHED = "fetched"
    PARSED = "parsed"
    NORMALIZED = "normalized"
    FAILED = "failed"


class ValidationStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"
    SKIP = "skip"


class MetricStatus(StrEnum):
    COMPUTED = "computed"
    NOT_COMPUTED = "not_computed"


class SectorType(StrEnum):
    GENERAL = "general"
    BANK = "bank"
    NBFC = "nbfc"
    INSURANCE = "insurance"


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class Period:
    """A dated period; INSTANT periods have start == end."""

    start: date
    end: date
    type: PeriodType

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"period start {self.start} is after end {self.end}")
        if self.type is PeriodType.INSTANT and self.start != self.end:
            raise ValueError("an INSTANT period must have start == end")


def to_decimal(value: str | int | Decimal) -> Decimal:
    """str | int | Decimal -> finite Decimal; floats (and bools) are rejected."""
    if isinstance(value, float) or isinstance(value, bool):
        raise TypeError(f"money must not be a {type(value).__name__}; pass a str, int or Decimal")
    if not isinstance(value, (str, int, Decimal)):
        raise TypeError(f"cannot convert {type(value).__name__} to Decimal")
    try:
        result = Decimal(value.strip() if isinstance(value, str) else value)
    except InvalidOperation as exc:
        raise ValueError(f"not a valid decimal: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"decimal must be finite: {value!r}")
    return result


def decimal_to_text(value: Decimal) -> str:
    """Decimal -> plain-notation TEXT for SQLite (no exponent, exact)."""
    return format(to_decimal(value), "f")


def text_to_decimal(text: str) -> Decimal:
    """TEXT from SQLite -> Decimal."""
    return to_decimal(text)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string for created_at columns."""
    return datetime.now(UTC).isoformat(timespec="seconds")
