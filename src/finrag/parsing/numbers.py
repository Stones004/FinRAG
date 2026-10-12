"""Clean numbers as printed in statements: brackets, Indian grouping, dashes."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# A dash alone (any common dash character) or the word NIL means "nothing reported".
_NULL_TOKEN = re.compile(r"^[-‐-―−]+$|^nil$", re.IGNORECASE)
_NUMBER = re.compile(r"^\(?[-−]?\d[\d,]*(?:\.\d+)?\)?$")
_TRAILING_MARKS = "*#^†‡"


class NotANumber(ValueError):
    """Raised when text is neither a number nor a null marker."""


def _strip(text: str) -> str:
    """Trim spaces, footnote marks, a trailing percent sign and spaces inside brackets."""
    trimmed = text.strip().rstrip(_TRAILING_MARKS).strip().removesuffix("%").strip()
    return re.sub(r"\(\s+|\s+\)", lambda m: m.group().strip(), trimmed)


def is_null_token(text: str) -> bool:
    """str -> True for a dash or NIL."""
    return bool(_NULL_TOKEN.match(_strip(text)))


def is_number_token(text: str) -> bool:
    """str -> True if it is a printed number or a null marker (dash)."""
    cleaned = _strip(text)
    if not cleaned:
        return False
    return bool(_NUMBER.match(cleaned)) or is_null_token(cleaned)


def parse_number(text: str) -> Decimal | None:
    """'(1,234.50)' -> Decimal('-1234.50'); '1,23,456' -> 123456; a dash or NIL -> None.

    Raises NotANumber for anything else. Never guesses (no letter-for-digit repair).
    """
    cleaned = _strip(text)
    if is_null_token(cleaned):
        return None
    if not _NUMBER.match(cleaned):
        raise NotANumber(f"not a number: {text!r}")
    negative = cleaned.startswith("(") or "-" in cleaned or "−" in cleaned
    digits = cleaned.strip("()").lstrip("-−").replace(",", "")
    try:
        value = Decimal(digits)
    except InvalidOperation as exc:  # pragma: no cover - the regex already guarantees digits
        raise NotANumber(f"not a number: {text!r}") from exc
    return -value if negative else value
