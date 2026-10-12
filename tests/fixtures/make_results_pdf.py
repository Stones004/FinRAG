"""Build a synthetic one-page results PDF with a statement table (no real company data).

The table has a serial-number column, a label column and five period columns: three under
"Quarter ended" and two under "Year ended", numbers right-aligned like a real filing.
Run as a script to write it: `python tests/fixtures/make_results_pdf.py out.pdf`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pymupdf

FONT = "helv"
SIZE = 9
LABEL_X = 70
# Right edges of the five numeric columns (points).
COLUMN_RIGHT = (330.0, 400.0, 470.0, 540.0, 610.0)
PITCH = 14.0
TOP = 150.0

# (label, five values as printed). "-" is a dash; (x) is a negative.
ROWS: list[tuple[str, tuple[str, ...]]] = [
    ("Revenue from operations", ("7,737.02", "8,049.25", "4,146.51", "21,835.63", "11,490.21")),
    ("Other income", ("102.35", "66.35", "24.98", "188.42", "93.90")),
    ("Total income", ("7,839.37", "8,115.60", "4,171.49", "22,024.05", "11,584.11")),
    ("Cost of materials consumed", ("6,236.92", "6,690.02", "3,232.71", "17,154.80", "9,121.96")),
    ("Employee benefits expense", ("214.33", "235.56", "183.15", "802.85", "623.37")),
    ("Exceptional items (net)", ("-", "-", "-", "-", "-")),
    ("Change in inventories", ("(78.72)", "(9.13)", "(9.51)", "128.11", "(286.88)")),
    ("Profit before tax", ("948.22", "980.66", "573.91", "2,765.20", "1,530.00")),
    ("Profit for the period", ("705.93", "733.11", "426.13", "2,058.16", "1,137.51")),
]
TITLE = "STATEMENT OF STANDALONE FINANCIAL RESULTS FOR QUARTER AND YEAR ENDED MARCH 31, 2026"
HEADER_DATES = ("31-03-2026", "31-12-2025", "31-03-2025", "31-03-2026", "31-03-2025")


@dataclass(frozen=True)
class Layout:
    """Where things were drawn, so tests can assert on positions if they need to."""

    first_row_y: float
    pitch: float


def _right(page: pymupdf.Page, right: float, y: float, text: str) -> None:
    """Draw text so that its right edge is at x = right."""
    width = pymupdf.get_text_length(text, fontname=FONT, fontsize=SIZE)
    page.insert_text((right - width, y), text, fontname=FONT, fontsize=SIZE)


def _centre(page: pymupdf.Page, centre: float, y: float, text: str) -> None:
    """Draw text centred on x = centre."""
    width = pymupdf.get_text_length(text, fontname=FONT, fontsize=SIZE)
    page.insert_text((centre - width / 2, y), text, fontname=FONT, fontsize=SIZE)


def build_pdf(
    unit_line: str = "(All amounts in Rs. in crore, unless otherwise stated)",
    title: str = TITLE,
    rows: list[tuple[str, tuple[str, ...]]] | None = None,
    footer_number: str | None = "6",
    columns: int = 5,
    kind_labels: bool = True,
    dates: bool = True,
    header_dates: tuple[str, ...] = HEADER_DATES,
    refer_note: bool = False,
) -> bytes:
    """Return a one-page results PDF; `rows` replaces the default table body.

    `columns` limits how many period columns are drawn (headers and numbers); `kind_labels`
    and `dates` switch the "Quarter ended / Year ended" row and the date row off.
    """
    body = rows if rows is not None else ROWS
    doc = pymupdf.open()
    page = doc.new_page(width=700, height=842)
    page.insert_text((LABEL_X, 60), "ALPHA INDUSTRIES LIMITED", fontname=FONT, fontsize=SIZE)
    page.insert_text((LABEL_X, 80), unit_line, fontname=FONT, fontsize=SIZE)
    page.insert_text((LABEL_X, 100), title, fontname=FONT, fontsize=SIZE)
    if kind_labels:
        _centre(page, COLUMN_RIGHT[1] - 35, 120, "Quarter ended")
        _centre(page, (COLUMN_RIGHT[3] + COLUMN_RIGHT[4]) / 2 - 35, 120, "Year ended")
    if dates:
        for right, text in zip(COLUMN_RIGHT[:columns], header_dates, strict=False):
            _centre(page, right - 35, 134, text)
    if refer_note:  # "Audited (Refer Note -4)" under two dates: a number-like token in a header
        for right in (COLUMN_RIGHT[2], COLUMN_RIGHT[4]):
            _centre(page, right - 35, 143, "Audited (Refer Note -4)")
    page.insert_text((LABEL_X, 134), "Particulars", fontname=FONT, fontsize=SIZE)
    y = TOP
    for label, values in body:
        page.insert_text((LABEL_X, y), label, fontname=FONT, fontsize=SIZE)
        for right, text in zip(COLUMN_RIGHT[:columns], values, strict=False):
            _right(page, right, y, text)
        y += PITCH
    if footer_number:
        _right(page, COLUMN_RIGHT[-1], 800, footer_number)
    data = doc.tobytes()
    doc.close()
    return data


if __name__ == "__main__":
    with open(sys.argv[1], "wb") as handle:
        handle.write(build_pdf())
