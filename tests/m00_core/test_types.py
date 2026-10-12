from datetime import date
from decimal import Decimal

import pytest

from finrag.core.types import (
    Period,
    PeriodType,
    decimal_to_text,
    is_valid_isin,
    looks_like_isin,
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


@pytest.mark.parametrize(
    "isin", ["INE0NT901020", "INE524A01029", "INE040A01034", "INE040A01018", " ine040a01034 "]
)
def test_is_valid_isin_accepts_real_isins(isin: str) -> None:
    assert is_valid_isin(isin)


@pytest.mark.parametrize(
    "text",
    ["INEONT901020", "INE524A01028", "INE524A0102", "524A01029XYZ", "GABRIEL", "", "INE524A0102A"],
)
def test_is_valid_isin_rejects_bad_isins(text: str) -> None:
    assert not is_valid_isin(text)


def test_looks_like_isin_ignores_the_check_digit() -> None:
    assert looks_like_isin("INEONT901020")  # shape is right, checksum is wrong
    assert not looks_like_isin("GABRIEL")
    assert not looks_like_isin("500999")
