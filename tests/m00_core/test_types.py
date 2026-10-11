from datetime import date
from decimal import Decimal

import pytest

from finrag.core.types import (
    Period,
    PeriodType,
    decimal_to_text,
    text_to_decimal,
    to_decimal,
)


@pytest.mark.parametrize("bad", [0.1, 1.0, True])
def test_to_decimal_rejects_floats_and_bools(bad: object) -> None:
    with pytest.raises(TypeError):
        to_decimal(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity"])
def test_to_decimal_rejects_non_numbers(bad: str) -> None:
    with pytest.raises(ValueError):
        to_decimal(bad)


@pytest.mark.parametrize(
    "text", ["0.1", "-1234567890123.456789", "1E+3", "0", "123456789012345678"]
)
def test_text_round_trip_is_exact(text: str) -> None:
    value = to_decimal(text)
    assert text_to_decimal(decimal_to_text(value)) == value
    assert "E" not in decimal_to_text(value)


def test_to_decimal_accepts_int_and_decimal() -> None:
    assert to_decimal(5) == Decimal(5)
    assert to_decimal(Decimal("1.5")) == Decimal("1.5")


def test_period_validation() -> None:
    with pytest.raises(ValueError):
        Period(date(2026, 1, 2), date(2026, 1, 1), PeriodType.Q)
    with pytest.raises(ValueError):
        Period(date(2026, 1, 1), date(2026, 1, 2), PeriodType.INSTANT)
