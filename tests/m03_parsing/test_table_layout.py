"""Words -> table geometry, on hand-made word boxes (no PDFs)."""

from __future__ import annotations

import pytest

from finrag.core.config import get_settings
from finrag.parsing.sections import load_section_patterns
from finrag.parsing.table_layout import TableParams, Word, build_table, group_lines

PITCH = 12.0
LABEL_X = 40.0
RIGHTS = (300.0, 380.0)  # right edges of two numeric columns


def label_words(text: str, y: float) -> list[Word]:
    """Label text as words laid out from LABEL_X on the line at height y."""
    words, x = [], LABEL_X
    for part in text.split():
        width = 5.0 * len(part)
        words.append(Word(part, x, x + width, y, y + 8))
        x += width + 4
    return words


def number(text: str, right: float, y: float) -> Word:
    """A right-aligned number word."""
    width = 5.0 * len(text)
    return Word(text, right - width, right, y, y + 8)


def row(label: str, values: tuple[str, ...], y: float, rights=RIGHTS) -> list[Word]:
    return label_words(label, y) + [number(v, r, y) for v, r in zip(values, rights, strict=False)]


def header(y: float = 20.0) -> list[Word]:
    """Dates over the two columns, as a header line above the data."""
    return [
        Word("31-03-2026", 255.0, 300.0, y, y + 8),
        Word("31-03-2025", 335.0, 380.0, y, y + 8),
    ]


@pytest.fixture
def params() -> TableParams:
    pdf = get_settings().parsing.pdf
    return TableParams(
        pdf.row_tolerance_pt,
        pdf.column_tolerance_pt,
        pdf.min_table_rows,
        pdf.header_max_lines,
        pdf.column_min_support_ratio,
    )


@pytest.fixture
def patterns():
    return load_section_patterns()


def table_of(words, patterns, params):
    table = build_table(words, patterns, params)
    assert table is not None
    return table


def test_lines_are_grouped_by_vertical_position(params) -> None:
    words = [
        Word("b", 50, 60, 100.5, 108),
        Word("a", 10, 20, 100, 108),
        Word("c", 10, 20, 130, 138),
    ]
    lines = group_lines(words, params.row_tolerance)
    assert [[w.text for w in line.words] for line in lines] == [["a", "b"], ["c"]]


def test_basic_rows_and_columns(patterns, params) -> None:
    words = header()
    for i, (label, values) in enumerate(
        [
            ("Revenue", ("1,000.50", "900.25")),
            ("Other income", ("10.00", "-")),
            ("Total", ("(5.5)", "3")),
        ]
    ):
        words += row(label, values, 50 + PITCH * i)
    table = table_of(words, patterns, params)
    assert len(table.columns) == 2
    assert [(r.label, r.cells) for r in table.rows] == [
        ("Revenue", {0: "1,000.50", 1: "900.25"}),
        ("Other income", {0: "10.00", 1: "-"}),
        ("Total", {0: "(5.5)", 1: "3"}),
    ]


def test_too_few_numeric_rows_is_no_table(patterns, params) -> None:
    words = row("Revenue", ("1.00", "2.00"), 50) + row("Cost", ("1.00", "2.00"), 62)
    assert build_table(words, patterns, params) is None


def test_stray_number_inside_a_label_stays_in_the_label(patterns, params) -> None:
    words = header()
    labels = ["Paid up share capital (face value of 2 per share)", "Other equity", "Total", "More"]
    for i, label in enumerate(labels):
        words += row(label, ("1.00", "2.00"), 50 + PITCH * i)
    table = table_of(words, patterns, params)
    assert table.rows[0].label == "Paid up share capital (face value of 2 per share)"
    assert table.rows[0].cells == {0: "1.00", 1: "2.00"}


def test_wrapped_label_above_the_numbers_is_joined(patterns, params) -> None:
    words = header() + row("Revenue", ("1.00", "2.00"), 50)
    words += label_words("Change in inventories of finished goods and", 62)
    words += row("work in progress", ("3.00", "4.00"), 74)
    words += row("Total", ("5.00", "6.00"), 86) + row("More", ("5.00", "6.00"), 98)
    table = table_of(words, patterns, params)
    assert table.rows[1].label == "Change in inventories of finished goods and work in progress"
    assert table.rows[1].cells == {0: "3.00", 1: "4.00"}


def test_heading_lines_are_not_joined_to_the_next_label(patterns, params) -> None:
    words = header() + row("Revenue", ("1.00", "2.00"), 50)
    words += label_words("EXPENSES", 62)
    words += row("Cost of goods", ("3.00", "4.00"), 74)
    words += row("Total", ("5.00", "6.00"), 86) + row("More", ("5.00", "6.00"), 98)
    table = table_of(words, patterns, params)
    assert [r.label for r in table.rows][:2] == ["Revenue", "Cost of goods"]


def test_numbers_centred_between_wrapped_label_lines(patterns, params) -> None:
    words = header() + row("Revenue", ("1.00", "2.00"), 50)
    words += label_words("Changes in inventories of goods and", 62)
    words += [number("75.48", RIGHTS[0], 68), number("(251.84)", RIGHTS[1], 68)]
    words += label_words("stock-in-trade", 74)
    words += row("Total", ("5.00", "6.00"), 90) + row("More", ("5.00", "6.00"), 102)
    table = table_of(words, patterns, params)
    wrapped = next(r for r in table.rows if r.label.startswith("Changes"))
    assert wrapped.label == "Changes in inventories of goods and stock-in-trade"
    assert wrapped.cells[0] == "75.48" and wrapped.cells[1] == "(251.84)"


def test_dates_in_the_header_are_not_numbers(patterns, params) -> None:
    words = [
        Word("March", 215, 240, 20, 28),
        Word("31,", 244, 255, 20, 28),
        Word("2026", 262, 300, 20, 28),
        Word("March", 295, 320, 20, 28),
        Word("31,", 324, 335, 20, 28),
        Word("2025", 342, 380, 20, 28),
    ]
    for i in range(4):
        words += row(f"Row {i}", ("1.00", "2.00"), 50 + PITCH * i)
    table = table_of(words, patterns, params)
    assert len(table.rows) == 4  # the header's years did not become a data row
    assert [h.text for h in table.header_lines] == ["March 31, 2026 March 31, 2025"]


def test_footer_page_number_is_dropped(patterns, params) -> None:
    words = header()
    for i in range(5):
        words += row(f"Row {i}", ("1.00", "2.00"), 50 + PITCH * i)
    words += [number("6", RIGHTS[1], 700)]
    table = table_of(words, patterns, params)
    assert len(table.rows) == 5


def test_note_reference_is_not_part_of_the_label(patterns, params) -> None:
    rights = (250.0, 300.0, 380.0)  # first column is a note column of small numbers
    words = header()
    for i, (note, label) in enumerate(
        [("30", "Revenue"), ("35a", "Employee cost"), ("31", "Other"), ("32", "Tax")]
    ):
        words += label_words(label, 50 + PITCH * i)
        words += [number(note, rights[0], 50 + PITCH * i)]
        words += [
            number("1.00", rights[1], 50 + PITCH * i),
            number("2.00", rights[2], 50 + PITCH * i),
        ]
    words += row("Total", ("5.00", "6.00"), 110, rights=rights[1:])
    table = table_of(words, patterns, params)
    assert [r.label for r in table.rows] == ["Revenue", "Employee cost", "Other", "Tax", "Total"]
    assert table.rows[1].cells[0] == "35a"
