"""100x / 0.01x slips caught by a column sum check (the real Netweb Q1 FY26 profit case)."""

from __future__ import annotations

from datetime import date

import pytest

from finrag.core.config import get_settings
from finrag.core.types import PeriodType
from finrag.parsing.pdf_tables import ColumnPeriod, sum_shifted
from finrag.parsing.sections import load_section_patterns
from finrag.parsing.table_layout import Column, Row, Table

PL = "profit_and_loss"
# Netweb Q2 FY26 results scan (FIL-adc5c2c447ce, page 3), columns: Q2 FY26, Q1 FY26, Q2 FY25,
# H1 FY26, H1 FY25, FY25. Printed in millions. Q1 FY26 profit was read as "30479" (304.79).
PBT = ["420.26", "416.06", "350.86", "836.32", "554.94", "1530.00"]
TAX = ["105.93", "111.27", "89.03", "217.20", "140.74", "392.49"]
PAT = ["314.33", "304.79", "261.83", "619.12", "414.20", "1137.51"]
ENDS = [
    date(2025, 9, 30),
    date(2025, 6, 30),
    date(2024, 9, 30),
    date(2025, 9, 30),
    date(2024, 9, 30),
    date(2025, 3, 31),
]


def table(pat: list[str], pbt: list[str] = PBT) -> Table:
    """A P&L table with the three rows of the profit_after_tax check."""
    rows = [
        Row(24, "Profit before tax (5-6)", dict(enumerate(pbt))),
        Row(26, "Tax expense (a) Current tax", dict(enumerate(["108.77", "109.87"]))),
        Row(29, "Total tax expense", dict(enumerate(TAX))),
        Row(30, "Profit for the period / year (7-8)", dict(enumerate(pat))),
    ]
    return Table([Column(float(i), 6) for i in range(6)], rows, [])


def periods() -> list[ColumnPeriod]:
    return [ColumnPeriod(end, PeriodType.Q, "") for end in ENDS]


def found(pat: list[str], pbt: list[str] = PBT, statement: str = PL) -> set[tuple[int, int]]:
    return sum_shifted(
        table(pat, pbt), periods(), statement, load_section_patterns().column_checks, get_settings()
    )


def test_a_correct_table_has_no_shifted_cell() -> None:
    assert found(PAT) == set()


def test_the_real_case_profit_read_100x_too_large_is_found() -> None:
    bad = list(PAT)
    bad[1] = "30479"  # as OCR read it; 304.79 x 100
    assert found(bad) == {(30, 1)}


def test_the_same_slip_with_the_usual_decimals_is_found_too() -> None:
    """The format check sees no outlier in "30479.00"; the sum check still does."""
    bad = list(PAT)
    bad[1] = "30479.00"
    assert found(bad) == {(30, 1)}


def test_a_value_100x_too_small_is_found() -> None:
    bad = list(PAT)
    bad[3] = "6.19"
    assert found(bad) == {(30, 3)}


def test_a_shifted_part_is_found_not_the_total() -> None:
    bad_pbt = list(PBT)
    bad_pbt[4] = "5.55"  # 554.94 read as 5.55
    assert found(PAT, bad_pbt) == {(24, 4)}


def test_an_ordinary_mismatch_is_not_a_shift() -> None:
    bad = list(PAT)
    bad[1] = "310.00"  # wrong, but not by a factor of 100
    assert found(bad) == set()


def test_rounding_differences_are_tolerated() -> None:
    near = list(PAT)
    near[1] = "304.80"
    assert found(near) == set()


def test_a_check_applies_only_to_its_statement() -> None:
    bad = list(PAT)
    bad[1] = "30479"
    assert found(bad, statement="cash_flow") == set()


def test_a_missing_required_row_skips_the_check() -> None:
    t = table(PAT)
    t.rows = [r for r in t.rows if r.label != "Total tax expense"]
    bad = sum_shifted(t, periods(), PL, load_section_patterns().column_checks, get_settings())
    assert bad == set()


def test_balance_sheet_slip_flags_both_sides_of_the_equality() -> None:
    rows = [
        Row(1, "Total Assets", {0: "20,105.72", 1: "17,860.14"}),
        Row(2, "Total Equity and Liabilities", {0: "2010572", 1: "17,860.14"}),
    ]
    t = Table([Column(0.0, 2), Column(1.0, 2)], rows, [])
    out = sum_shifted(
        t,
        [ColumnPeriod(date(2025, 9, 30), PeriodType.INSTANT, "")] * 2,
        "balance_sheet",
        load_section_patterns().column_checks,
        get_settings(),
    )
    assert out == {(1, 0), (2, 0)}  # either side could be the 100x one: both are capped


@pytest.mark.parametrize("cell", ["30479", "30479.00"])
def test_the_shifted_cell_gets_the_capped_confidence_in_the_facts(cell: str) -> None:
    from finrag.core.types import Basis
    from finrag.parsing.page_classifier import PageLabel
    from finrag.parsing.pdf_tables import _facts_for_table
    from finrag.parsing.pdf_text import PageText

    bad = list(PAT)
    bad[1] = cell
    t = table(bad)
    settings = get_settings()
    shifted = found(bad)
    page = PageText(3, "", "ocr", "tesseract", 0, 0.9, False)
    label = PageLabel(3, PL, Basis.STANDALONE, "million", 5)
    facts = _facts_for_table(
        t,
        periods(),
        page,
        label,
        Basis.STANDALONE,
        load_section_patterns(),
        settings,
        0.79,
        set(),
        shifted,
    )
    by_loc = {f.source_loc: f.confidence for f in facts}
    assert by_loc["3:30:2"] == settings.parsing.pdf.sum_shift_confidence
    assert by_loc["3:30:1"] == pytest.approx(0.79)
