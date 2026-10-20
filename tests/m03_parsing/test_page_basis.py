"""A statement page's own heading (standalone / consolidated) beats the filing's label."""

from __future__ import annotations

import pymupdf
import pytest

from finrag.core.types import Basis
from finrag.parsing.page_classifier import basis_of, classify_pages
from finrag.parsing.pdf_tables import extract_statement_tables
from finrag.parsing.pdf_text import extract_pdf_text
from finrag.parsing.sections import load_section_patterns
from finrag.parsing.text_quality import load_vocabulary

FS = 8
RIGHTS = [389.0, 437.0, 485.0, 534.0]
ROWS = [
    ("Revenue from operations", ["12,742.47", "12,097.30", "10,716.81", "45,773.41"]),
    ("Other income", ["186.79", "114.01", "71.15", "518.16"]),
    ("Total Income (I+II)", ["12,929.26", "12,211.31", "10,787.96", "46,291.57"]),
    ("Total expenses", ["12,230.68", "11,479.37", "10,310.21", "43,790.90"]),
    ("Profit before tax", ["698.58", "731.94", "477.75", "2,500.67"]),
    ("Profit for the period", ["596.13", "559.20", "399.80", "2,124.30"]),
]
STANDALONE = (
    "STATEMENT OF STANDALONE UNAUDITED FINANCIAL RESULTS FOR THE QUARTER ENDED JUNE 30, 2026"
)
CONSOLIDATED = STANDALONE.replace("STANDALONE", "CONSOLIDATED")
NOTE = "The Scheme was given effect in the consolidated financial results, as prescribed."


def results_page(title: str | None, title_last: bool = True, note: str | None = None) -> bytes:
    """One P&L page. The title is at the top of the page; with `title_last` it is written to the
    content stream last, so a text layer lists it after the table (as Gabriel's PDFs do)."""
    doc = pymupdf.open()
    page = doc.new_page()
    top = [
        ("GABRIEL INDIA LIMITED", 60, 40),
        ("Registered office, Pune-410501", 60, 52),
        ("(Amount in Rs. Million)", 60, 80),
        ("Quarter ended", 388, 95),
        ("Year ended", 492, 95),
        ("Particulars", 60, 118),
    ]
    for text, x, y in top:
        page.insert_text((x, y), text, fontsize=FS)
    for right, text in zip(
        RIGHTS, ["30.06.2026", "31.03.2026", "30.06.2025", "31.03.2026"], strict=True
    ):
        width = pymupdf.get_text_length(text, fontsize=FS)
        page.insert_text((right - width, 106), text, fontsize=FS)
    y = 140.0
    for label, values in ROWS:
        page.insert_text((60, y), label, fontsize=FS)
        for right, value in zip(RIGHTS, values, strict=True):
            width = pymupdf.get_text_length(value, fontsize=FS)
            page.insert_text((right - width, y), value, fontsize=FS)
        y += 12
    if note:
        page.insert_text((60, y + 20), note, fontsize=FS)
    if title and title_last:
        page.insert_text((60, 66), title, fontsize=FS)
    data = doc.tobytes()
    doc.close()
    return data


def labels_of(data: bytes):
    pages = extract_pdf_text(data, "financial_results", None, load_vocabulary()).pages
    patterns = load_section_patterns()
    return pages, patterns, classify_pages(pages, patterns)


def test_the_title_is_not_among_the_first_lines_of_the_text_layer() -> None:
    """The reason the first-lines scan missed it."""
    pages, patterns, _ = labels_of(results_page(STANDALONE))
    lines = [ln for ln in pages[0].text.splitlines() if ln.strip()]
    assert not any("STANDALONE" in ln for ln in lines[:15])
    assert any("STANDALONE" in ln for ln in lines[15:])


@pytest.mark.parametrize(
    "title, expected",
    [(STANDALONE, Basis.STANDALONE), (CONSOLIDATED, Basis.CONSOLIDATED), (None, Basis.UNKNOWN)],
)
def test_the_page_heading_gives_the_page_basis_wherever_the_text_layer_puts_it(
    title, expected
) -> None:
    _, _, labels = labels_of(results_page(title))
    assert labels[0].statement_type == "profit_and_loss" and labels[0].basis is expected


def test_a_note_that_mentions_the_other_basis_is_not_a_heading() -> None:
    _, _, labels = labels_of(results_page(STANDALONE, note=NOTE))
    assert labels[0].basis is Basis.STANDALONE


def test_two_titles_on_one_page_give_no_page_basis() -> None:
    patterns = load_section_patterns()
    text = f"{STANDALONE}\nrows\n{CONSOLIDATED}"
    assert basis_of(text, patterns, 0) is Basis.UNKNOWN


@pytest.mark.parametrize(
    "filing, title, expected",
    [
        (Basis.CONSOLIDATED, STANDALONE, Basis.STANDALONE),  # the page's own heading wins
        (Basis.STANDALONE, CONSOLIDATED, Basis.CONSOLIDATED),
        (Basis.CONSOLIDATED, None, Basis.CONSOLIDATED),  # no heading: the filing's label
        (Basis.STANDALONE, None, Basis.STANDALONE),
    ],
)
def test_the_facts_take_the_page_basis_else_the_filing_basis(filing, title, expected) -> None:
    data = results_page(title)
    pages, patterns, labels = labels_of(data)
    result = extract_statement_tables(data, pages, labels, patterns, filing)
    assert result.facts and {f.basis for f in result.facts} == {expected.value}
