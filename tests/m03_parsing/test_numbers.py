"""Number cleaning: brackets, Indian grouping, dashes."""

from __future__ import annotations

from decimal import Decimal

import pytest

from finrag.parsing.numbers import NotANumber, is_null_token, is_number_token, parse_number


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(1,234)", Decimal("-1234")),
        ("(1,234.50)", Decimal("-1234.50")),
        ("1,23,456", Decimal("123456")),
        ("12,34,56,789.10", Decimal("123456789.10")),
        ("-5.25", Decimal("-5.25")),
        ("−5", Decimal("-5")),
        ("7,737.02", Decimal("7737.02")),
        ("0.55", Decimal("0.55")),
        ("12.43*", Decimal("12.43")),
        (" 42 ", Decimal("42")),
        ("( 0.55 )", Decimal("-0.55")),
    ],
)
def test_parse_number(text: str, expected: Decimal) -> None:
    assert parse_number(text) == expected


@pytest.mark.parametrize("text", ["—", "–", "-", "--", "nil", "NIL", " - "])
def test_dash_and_nil_are_null(text: str) -> None:
    assert parse_number(text) is None
    assert is_null_token(text)
    assert is_number_token(text)


@pytest.mark.parametrize("text", ["abc", "", "12a", "1.2.3", "I", "1 3", "Rs", "31-03-2026", "35a"])
def test_not_a_number(text: str) -> None:
    assert not is_number_token(text) or text == ""
    with pytest.raises(NotANumber):
        parse_number(text)


def test_never_repairs_letters_for_digits() -> None:
    with pytest.raises(NotANumber):
        parse_number("l3.88")  # an OCR 'l' for '1' is not guessed
    with pytest.raises(NotANumber):
        parse_number("7,7O7.02")


def test_result_is_decimal_not_float() -> None:
    assert isinstance(parse_number("0.1"), Decimal)
    assert parse_number("0.1") + parse_number("0.2") == Decimal("0.3")
