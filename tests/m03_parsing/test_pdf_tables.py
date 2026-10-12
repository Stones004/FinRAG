"""Statement tables in PDFs: facts, periods, units, confidence, review items, OCR path."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pdfplumber
import pymupdf
import pytest

from finrag.core.config import get_settings
from finrag.core.types import Basis
from finrag.parsing.page_classifier import classify_pages
from finrag.parsing.pdf_tables import _native_words, extract_statement_tables
from finrag.parsing.pdf_text import PageText, extract_pdf_text
from finrag.parsing.sections import load_section_patterns
from finrag.parsing.text_quality import load_vocabulary

Q4, Q3, Q4_PRIOR = (
    (date(2026, 1, 1), date(2026, 3, 31)),
    (date(2025, 10, 1), date(2025, 12, 31)),
    (
        date(2025, 1, 1),
        date(2025, 3, 31),
    ),
)
FY, FY_PRIOR = (date(2025, 4, 1), date(2026, 3, 31)), (date(2024, 4, 1), date(2025, 3, 31))
PERIODS = [Q4, Q3, Q4_PRIOR, FY, FY_PRIOR]


@pytest.fixture
def patterns():
    return load_section_patterns()


def read(data: bytes, basis: Basis = Basis.UNKNOWN):
    """PDF bytes -> (pages, labels, TablesResult) through the real text/classify/table steps."""
    pages = extract_pdf_text(data, "financial_results", None, load_vocabulary()).pages
    patterns = load_section_patterns()
    labels = classify_pages(pages, patterns)
    return pages, labels, extract_statement_tables(data, pages, labels, patterns, basis)


def by_label(result, label: str):
    """The facts of one row label, in column order."""
    return sorted(
        (f for f in result.facts if f.label_raw == label),
        key=lambda f: int(f.source_loc.split(":")[2]),
    )


def test_pl_page_is_classified_and_values_match_exactly(kit) -> None:
    pages, labels, result = read(kit.results.build_pdf(), Basis.STANDALONE)
    assert labels[0].statement_type == "profit_and_loss"
    revenue = by_label(result, "Revenue from operations")
    assert [f.value for f in revenue] == [
        Decimal("7737.02"),
        Decimal("8049.25"),
        Decimal("4146.51"),
        Decimal("21835.63"),
        Decimal("11490.21"),
    ]
    assert len(result.facts) == len(kit.results.ROWS) * 5
    assert result.pages_read == [1] and result.pages_unread == []


def test_number_like_header_text_does_not_become_a_row_or_column(kit) -> None:
    plain = read(kit.results.build_pdf())[2]
    noted = read(kit.results.build_pdf(refer_note=True))[2]
    assert [(f.label_raw, f.value, f.period_end) for f in noted.facts] == [
        (f.label_raw, f.value, f.period_end) for f in plain.facts
    ]


def test_number_with_unusual_decimals_gets_low_confidence_and_a_review_item(kit) -> None:
    rows = [(label, vals) for label, vals in kit.results.ROWS]
    rows[7] = ("Profit before tax", ("94822", *rows[7][1][1:]))  # OCR lost the decimal point
    _, _, result = read(kit.results.build_pdf(rows=rows))
    cap = get_settings().parsing.pdf.format_outlier_confidence
    bad = by_label(result, "Profit before tax")[0]
    assert bad.value == Decimal("94822") and bad.confidence == cap
    assert all(f.confidence > cap for f in result.facts if f is not bad)
    (item,) = result.review_items
    assert [o["label"] for o in item["format_outliers"]] == ["Profit before tax"]


def test_clean_table_has_no_format_outliers(kit) -> None:
    assert read(kit.results.build_pdf())[2].review_items == []


def test_every_printed_value_is_parsed_exactly(kit) -> None:
    _, _, result = read(kit.results.build_pdf())
    for label, printed in kit.results.ROWS:
        facts = by_label(result, label)
        assert len(facts) == 5, label
        for fact, text in zip(facts, printed, strict=True):
            if text == "-":
                assert fact.value is None
            else:
                negative = text.startswith("(")
                number = Decimal(text.strip("()").replace(",", ""))
                assert fact.value == (-number if negative else number), (label, text)


def test_columns_get_their_periods(kit) -> None:
    _, _, result = read(kit.results.build_pdf())
    periods = [(f.period_start, f.period_end) for f in by_label(result, "Total income")]
    assert periods == PERIODS


def test_unit_line_gives_scale_and_unit(kit) -> None:
    _, labels, result = read(kit.results.build_pdf("(All amounts in Rs. in crore)"))
    assert labels[0].unit_hint == "crore"
    assert {(f.unit_raw, f.scale) for f in result.facts} == {("crore", 10_000_000)}


def test_million_and_lakh_scales(kit) -> None:
    million = read(kit.results.build_pdf("(All amounts in Indian Rupees in millions)"))[2]
    lakh = read(kit.results.build_pdf("(Rs. in lakhs)"))[2]
    assert {f.scale for f in million.facts} == {1_000_000}
    assert {f.scale for f in lakh.facts} == {100_000}


def test_no_unit_line_gives_no_scale(kit) -> None:
    _, _, result = read(kit.results.build_pdf("(all amounts as stated)"))
    assert {(f.unit_raw, f.scale) for f in result.facts} == {(None, None)}


def test_basis_from_the_page_beats_the_filing_basis(kit) -> None:
    _, _, result = read(kit.results.build_pdf(), Basis.CONSOLIDATED)
    assert {f.basis for f in result.facts} == {"standalone"}  # the title says STANDALONE


def test_filing_basis_is_used_when_the_page_names_none(kit) -> None:
    data = kit.results.build_pdf(
        title="STATEMENT OF FINANCIAL RESULTS FOR QUARTER AND YEAR ENDED MARCH 31, 2026"
    )
    _, _, result = read(data, Basis.CONSOLIDATED)
    assert {f.basis for f in result.facts} == {"consolidated"}


def test_fact_fields_and_source_loc(kit) -> None:
    _, _, result = read(kit.results.build_pdf())
    fact = by_label(result, "Other income")[1]
    assert fact.concept_raw is None
    assert (fact.extractor, fact.ocr_backend, fact.decimals) == ("pdf_table", None, None)
    page, row, col = fact.source_loc.split(":")
    assert (page, col) == ("1", "2") and int(row) > 0
    assert fact.dimensions == ()


def test_footer_page_number_is_not_a_fact(kit) -> None:
    _, _, result = read(kit.results.build_pdf(footer_number="6"))
    assert all(f.label_raw != "" and f.value != Decimal("6") for f in result.facts)
    assert len(result.facts) == len(kit.results.ROWS) * 5


def test_confidence_is_high_for_a_clean_table(kit) -> None:
    _, _, result = read(kit.results.build_pdf())
    assert all(
        f.confidence >= get_settings().parsing.pdf.min_table_confidence for f in result.facts
    )
    assert result.review_items == []


def test_missing_period_kind_gives_low_confidence_review_item_and_keeps_facts(kit) -> None:
    _, _, result = read(kit.results.build_pdf(kind_labels=False))
    threshold = get_settings().parsing.pdf.min_table_confidence
    assert result.facts, "facts are still stored"
    assert all(f.confidence < threshold for f in result.facts)
    assert all(f.period_start is None and f.period_end is not None for f in result.facts)
    (item,) = result.review_items
    assert item["page"] == 1 and item["statement_type"] == "profit_and_loss"
    assert item["confidence"] < threshold
    assert item["components"]["header_found"] == 0.0


def test_page_without_date_headers_is_unread_and_queued(kit) -> None:
    _, _, result = read(kit.results.build_pdf(dates=False, kind_labels=False))
    assert result.facts == []
    assert result.pages_unread == [
        {
            "page": 1,
            "statement_type": "profit_and_loss",
            "reason": "no column header with a period date",
        }
    ]
    assert result.review_items[0]["confidence"] == 0.0


def test_balance_sheet_columns_are_instants(kit) -> None:
    rows = [
        ("Total non-current assets", ("8,017.65", "6,179.14")),
        ("Total current assets", ("13,061.65", "11,681.00")),
        ("Total assets", ("21,079.30", "17,860.14")),
        ("Total equity and liabilities", ("21,079.30", "17,860.14")),
    ]
    data = kit.results.build_pdf(
        title="BALANCE SHEET AS AT MARCH 31, 2026",
        rows=rows,
        columns=2,
        kind_labels=False,
        header_dates=("31-03-2026", "31-03-2025"),
    )
    _, labels, result = read(data)
    assert labels[0].statement_type == "balance_sheet"
    facts = by_label(result, "Total assets")
    assert [(f.period_start, f.period_end) for f in facts] == [
        (date(2026, 3, 31), date(2026, 3, 31)),
        (date(2025, 3, 31), date(2025, 3, 31)),
    ]


def test_page_with_no_table_is_unread_and_queued(kit) -> None:
    data = kit.results.build_pdf(
        rows=[("Total income", ("1.00",)), ("Profit before tax", ("2.00",))]
    )
    _, _, result = read(data)
    assert result.facts == []
    assert result.pages_unread[0]["reason"] == "no numeric columns found"
    assert len(result.review_items) == 1


def test_non_statement_pages_are_ignored(kit) -> None:
    data = kit.text_pdf(
        ["The board met five times during the year and discussed strategy at length."]
    )
    _, _, result = read(data)
    assert (result.facts, result.review_items, result.pages_read) == ([], [], [])


def test_rotated_native_page_is_not_trusted(kit) -> None:
    doc = pymupdf.open(stream=kit.results.build_pdf(), filetype="pdf")
    doc[0].set_rotation(90)
    _, _, result = read(doc.tobytes())
    assert result.facts == []
    assert "rotated page" in result.pages_unread[0]["reason"]


def ocr_page_from(data: bytes, backend: str = "tesseract") -> list[PageText]:
    """Re-express a native page as an OCR page carrying positioned words."""
    from finrag.parsing.ocr.base import OcrWord

    with pdfplumber.open(__import__("io").BytesIO(data)) as doc:
        words = tuple(
            OcrWord(w.text, w.x0, w.x1, w.top, w.bottom) for w in _native_words(doc.pages[0])
        )
    text = pymupdf.open(stream=data, filetype="pdf")[0].get_text()
    return [PageText(1, text, "ocr", backend, len(text), 0.9, False, words=words)]


@pytest.mark.parametrize("backend", ["tesseract", "chandra"])
def test_ocr_page_facts_carry_extractor_and_backend(kit, patterns, backend) -> None:
    data = kit.results.build_pdf()
    pages = ocr_page_from(data, backend)
    labels = classify_pages(pages, patterns)
    result = extract_statement_tables(data, pages, labels, patterns, Basis.STANDALONE)
    assert result.facts
    assert {(f.extractor, f.ocr_backend) for f in result.facts} == {("ocr", backend)}
    assert [f.value for f in by_label(result, "Revenue from operations")][0] == Decimal("7737.02")


def test_native_facts_never_name_an_ocr_backend(kit) -> None:
    _, _, result = read(kit.results.build_pdf())
    assert {f.ocr_backend for f in result.facts} == {None}


def test_ocr_page_without_words_is_unread(kit, patterns) -> None:
    data = kit.results.build_pdf()
    text = pymupdf.open(stream=data, filetype="pdf")[0].get_text()
    pages = [PageText(1, text, "ocr", "tesseract", len(text), 0.9, False, words=())]
    labels = classify_pages(pages, patterns)
    result = extract_statement_tables(data, pages, labels, patterns, Basis.STANDALONE)
    assert result.pages_unread[0]["reason"] == "OCR gave no words"


def test_inherited_unit_lowers_confidence_and_records_the_source_page(kit, patterns) -> None:
    data = kit.results.build_pdf("(all amounts as stated)")  # no unit line on this page
    pages = extract_pdf_text(data, "financial_results", None, load_vocabulary()).pages
    labels = classify_pages(pages, patterns)
    from dataclasses import replace

    inherited = [replace(labels[0], unit_hint="crore", unit_from_page=3)]
    result = extract_statement_tables(data, pages, inherited, patterns, Basis.STANDALONE)
    penalty = get_settings().parsing.pdf.unit_inherited_confidence_penalty
    own = read(kit.results.build_pdf())[2].facts[0].confidence
    assert {f.scale for f in result.facts} == {10_000_000}
    assert all(f.source_loc.endswith(";unit_page=3") for f in result.facts)
    assert result.facts[0].confidence == pytest.approx(own - penalty)
