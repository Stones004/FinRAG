"""Shared enums, the Period type and Decimal money helpers."""

from __future__ import annotations

import re
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


class SourceClass(StrEnum):
    """Where a canonical fact came from (drives source precedence in the fact store)."""

    XBRL = "xbrl"
    PDF = "pdf"
    OCR = "ocr"
    DERIVED = "derived"


class FilingStatus(StrEnum):
    FETCHED = "fetched"
    PARSED = "parsed"
    NORMALIZED = "normalized"
    FAILED = "failed"
    QUARANTINED = "quarantined"  # content conflicts with its metadata; kept, never parsed


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


class EventType(StrEnum):
    """corporate_events.event_type."""

    RENAME = "rename"
    MERGER = "merger"
    DEMERGER = "demerger"
    DELISTING = "delisting"
    BASIS_CHANGE = "basis_change"
    # M06: periods before the event are on the old accounting basis (see M07 `basis_change`)
    ACCOUNTING_POLICY_CHANGE = "accounting_policy_change"
    COMPARATIVE_RESTATEMENT = "comparative_restatement"


class AliasType(StrEnum):
    SYMBOL = "symbol"
    CODE = "code"
    NAME = "name"
    USER = "user"
    ISIN = "isin"
    SCRIP_CODE = "scrip_code"


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


ISIN_PATTERN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def looks_like_isin(text: str) -> bool:
    """str -> True if it has the ISIN shape (2 letters, 9 alphanumerics, 1 digit)."""
    return ISIN_PATTERN.fullmatch(text.strip().upper()) is not None


def is_valid_isin(text: str) -> bool:
    """str -> True if it has the ISIN shape and a correct Luhn check digit."""
    candidate = text.strip().upper()
    if not looks_like_isin(candidate):
        return False
    digits = "".join(str(int(char, 36)) for char in candidate)
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
