"""Column periods of a results PDF: the header type must fit the column's dates, tight columns
and ghost spaces must not break the table (found on Gabriel's Q1 FY27 results PDF)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pdfplumber
import pymupdf
import pytest

from finrag.core.config import get_settings
from finrag.core.types import Basis, PeriodType
from finrag.parsing.page_classifier import classify_pages
from finrag.parsing.pdf_tables import (
    _native_words,
    extract_statement_tables,
    ghost_spaces,
    kind_fits,
)
from finrag.parsing.pdf_text import extract_pdf_text
from finrag.parsing.sections import load_section_patterns
from finrag.parsing.table_layout import TableParams, Word, build_table, find_columns, group_lines
from finrag.parsing.text_quality import load_vocabulary

FS = 8
ROWS = [
    ("Revenue from operations", ["12,742.47", "12,097.30", "10,716.81", "45,773.41"]),
    ("Other income", ["186.79", "114.01", "71.15", "518.16"]),
    ("Total Income (I+II)", ["12,929.26", "12,211.31", "10,787.96", "46,291.57"]),
    ("Cost of materials consumed", ["9,487.25", "8,933.43", "7,834.66", "33,643.00"]),
    ("Employee benefits expense", ["856.21", "783.01", "815.29", "3,250.22"]),
    ("Depreciation and amortisation", ["323.02", "305.85", "306.55", "1,235.79"]),
    ("Other expenses", ["1,564.20", "1,457.08", "1,353.71", "5,660.89"]),
    ("Total expenses", ["12,230.68", "11,479.37", "10,310.21", "43,790.90"]),
    ("Profit before tax", ["698.58", "731.94", "477.75", "2,500.67"]),
    ("Profit for the period", ["596.13", "559.20", "399.80", "2,124.30"]),
]
RIGHTS = [389.0, 437.0, 485.0, 534.0]  # right edges of the four numeric columns (49 pt apart)
DATES = ["30.06.2026", "31.03.2026", "30.06.2025", "31.03.2026"]


def _right(page, text: str, right: float, y: float, ghost: bool = False) -> None:
    """Text right-aligned at `right`; with `ghost` a space glyph sits on its first digit."""
    width = pymupdf.get_text_length(text, fontsize=FS)
    x = right - width
    if ghost and len(text) > 2:
        first = pymupdf.get_text_length(text[0], fontsize=FS)
        page.insert_text((x, y), text[0], fontsize=FS)
        page.insert_text((x + first * 0.3, y), " ", fontsize=FS)
        page.insert_text((x + first, y), text[1:], fontsize=FS)
    else:
        page.insert_text((x, y), text, fontsize=FS)


def results_pdf(ghost_rows: tuple[int, ...] = (), address_number: bool = False) -> bytes:
    """One standalone P&L page: 'Quarter ended' over the middle of three quarter columns and
    'Year ended' over the fourth, as printed by Indian issuers."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 40.0
    if address_number:
        page.insert_text((470, y), "Pune-410501", fontsize=FS)
        y += 20
    page.insert_text(
        (60, y),
        "STATEMENT OF STANDALONE UNAUDITED FINANCIAL RESULTS FOR THE QUARTER ENDED JUNE 30, 2026",
        fontsize=FS,
    )
    page.insert_text((60, y + 14), "(Amount in Rs. Million)", fontsize=FS)
    page.insert_text((388, y + 28), "Quarter ended", fontsize=FS)
    page.insert_text((492, y + 28), "Year ended", fontsize=FS)
    for right, text in zip(RIGHTS, DATES, strict=True):
        _right(page, text, right, y + 40)
    page.insert_text((60, y + 52), "Particulars", fontsize=FS)
    y += 70
    for number, (label, values) in enumerate(ROWS):
        page.insert_text((60, y), label, fontsize=FS)
        for right, value in zip(RIGHTS, values, strict=True):
            _right(page, value, right, y, ghost=number in ghost_rows)
        y += 12
    data = doc.tobytes()
    doc.close()
    return data


def read(data: bytes):
    pages = extract_pdf_text(data, "financial_results", None, load_vocabulary()).pages
    patterns = load_section_patterns()
    labels = classify_pages(pages, patterns)
    return extract_statement_tables(data, pages, labels, patterns, Basis.STANDALONE), labels


def row(result, label: str):
    return sorted(
        (f for f in result.facts if f.label_raw.endswith(label)),
        key=lambda f: int(f.source_loc.split(":")[2]),
    )


# --- the period length must match the header type ------------------------------------------


@pytest.mark.parametrize(
    "end, kind, fits",
    [
        (date(2025, 6, 30), PeriodType.FY, False),  # twelve months to June is no March fiscal year
        (date(2025, 6, 30), PeriodType.Q, True),
        (date(2026, 3, 31), PeriodType.FY, True),
        (date(2026, 3, 31), PeriodType.Q, True),
        (date(2025, 9, 30), PeriodType.H, True),
        (date(2025, 12, 31), PeriodType.NINE_M, True),
        (date(2025, 12, 31), PeriodType.FY, False),
        (date(2025, 6, 30), PeriodType.H, False),
    ],
)
def test_a_kind_fits_only_a_date_that_ends_a_fiscal_period_of_that_length(end, kind, fits):
    assert kind_fits(end, kind, load_section_patterns()) is fits


def test_the_comparative_quarter_column_is_a_quarter_not_a_year() -> None:
    result, labels = read(results_pdf())
    assert labels[0].statement_type == "profit_and_loss"
    spans = [(f.period_start, f.period_end) for f in row(result, "Revenue from operations")]
    assert spans == [
        (date(2026, 4, 1), date(2026, 6, 30)),
        (date(2026, 1, 1), date(2026, 3, 31)),
        (date(2025, 4, 1), date(2025, 6, 30)),  # was 2024-07-01..2025-06-30
        (date(2025, 4, 1), date(2026, 3, 31)),
    ]


def test_two_columns_cannot_be_the_same_period() -> None:
    """Columns ending on the same 31 March: the one far from 'Year ended' is the quarter."""
    from finrag.parsing.pdf_tables import ColumnPeriod, _distinct

    patterns = load_section_patterns()
    periods = [
        ColumnPeriod(date(2026, 3, 31), PeriodType.Q, "q4"),
        ColumnPeriod(date(2025, 12, 31), PeriodType.Q, "q3"),
        ColumnPeriod(date(2025, 3, 31), PeriodType.FY, "q4 prior"),  # wrongly the year
        ColumnPeriod(date(2026, 3, 31), PeriodType.FY, "fy"),
        ColumnPeriod(date(2025, 3, 31), PeriodType.FY, "fy prior"),
    ]
    kinds = [(150.0, PeriodType.Q), (500.0, PeriodType.FY)]
    centres = [100.0, 150.0, 200.0, 480.0, 520.0]
    fixed = _distinct(periods, kinds, centres, patterns)
    assert [p.kind for p in fixed] == [
        PeriodType.Q,
        PeriodType.Q,
        PeriodType.Q,
        PeriodType.FY,
        PeriodType.FY,
    ]


# --- ghost spaces ------------------------------------------------------------------------------


def test_a_space_glyph_on_top_of_a_digit_does_not_cut_the_number() -> None:
    data = results_pdf(ghost_rows=(0, 3))
    page = pdfplumber.open(__import__("io").BytesIO(data)).pages[0]
    assert ghost_spaces(page.chars)  # the fixture really has them
    plain = [w["text"] for w in page.extract_words()]
    assert "1" in plain and "0,716.81" in plain  # pdfplumber alone cuts "10,716.81"
    merged = [w.text for w in _native_words(page)]
    assert "10,716.81" in merged and "0,716.81" not in merged


def test_values_with_ghost_spaces_are_read_whole() -> None:
    result, _ = read(results_pdf(ghost_rows=(0, 3)))
    assert [f.value for f in row(result, "Revenue from operations")] == [
        Decimal("12742.47"),
        Decimal("12097.30"),
        Decimal("10716.81"),
        Decimal("45773.41"),
    ]
    assert Decimal("33643.00") in [f.value for f in row(result, "Cost of materials consumed")]


# --- the first data row ------------------------------------------------------------------------


def test_a_postcode_above_the_header_is_not_the_first_data_row() -> None:
    result, _ = read(results_pdf(address_number=True))
    assert not result.pages_unread
    assert len(row(result, "Revenue from operations")) == 4
    assert all(f.period_end for f in result.facts)


# --- tight columns -----------------------------------------------------------------------------


def _words(rows: list[list[tuple[float, str]]]) -> list[Word]:
    out = []
    for n, cells in enumerate(rows):
        top = 100.0 + 12 * n
        for right, text in cells:
            out.append(Word(text, right - 4 * len(text), right, top, top + 8))
    return out


def test_narrow_columns_joined_by_stray_numbers_stay_separate_and_noise_is_not_a_column() -> None:
    rights = [347.0, 383.0, 420.0, 456.0]
    rows = []
    for n in range(14):
        cells = [(r + (n % 3) * 0.8, f"{100 + n}.{n:02d}") for r in rights]
        if n < 9:  # fragments of split numbers between the third and fourth column
            cells.append((432.0 + 3 * n, f"{n}.5"))
        if n < 3:  # numbers inside labels
            cells.append((144.0, "1"))
        rows.append(cells)
    s = get_settings().parsing.pdf
    lines = group_lines(_words(rows), s.row_tolerance_pt)
    columns = find_columns(
        lines,
        load_section_patterns(),
        s.column_tolerance_pt,
        s.min_table_rows,
        s.column_min_support_ratio,
    )
    assert len(columns) == 4
    assert all(abs(c.right - r) <= 3 for c, r in zip(columns, rights, strict=True))
    params = TableParams(
        s.row_tolerance_pt,
        s.column_tolerance_pt,
        s.min_table_rows,
        s.header_max_lines,
        s.column_min_support_ratio,
    )
    table = build_table(_words(rows), load_section_patterns(), params)
    assert table is not None and len(table.columns) == 4
    assert sum(len(r.cells) == 4 for r in table.rows) >= 12
