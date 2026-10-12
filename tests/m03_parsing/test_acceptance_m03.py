"""M03 acceptance criteria, one test per criterion (numbering follows docs/modules/M03_parsing.md).

Criterion 7 (golden totals from a real XBRL file) lives in test_golden_m03.py because it needs the
real filings; it is marked `golden` and skipped when they are absent.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
import yaml
from PIL import Image

from finrag.core.config import get_settings, reset_settings_cache
from finrag.core.db import tables, transaction
from finrag.core.types import Basis
from finrag.parsing.numbers import parse_number
from finrag.parsing.ocr.base import OcrUnavailable
from finrag.parsing.ocr.chandra import ChandraBackend, downscale
from finrag.parsing.ocr.factory import get_backend
from finrag.parsing.page_classifier import classify_pages, unit_hint_of
from finrag.parsing.pdf_tables import extract_statement_tables
from finrag.parsing.pdf_text import extract_pdf_text
from finrag.parsing.runner import run_parse
from finrag.parsing.sections import load_section_patterns
from finrag.parsing.text_quality import load_vocabulary
from finrag.parsing.xbrl_parser import parse_xbrl

CLEAN = (
    "Revenue from operations increased because the company sold more products to customers "
    "during the quarter. Management expects demand to remain strong across the business."
)
GARBLED = "Revmue fom operatons Prcfit befor tsx Finmce costs Deproiation mortietion expenss"


def read_pdf(data: bytes, backend=None, filing_type: str = "financial_results"):
    return extract_pdf_text(data, filing_type, backend, load_vocabulary()).pages


def set_ocr(isolated_repo, **changes) -> None:
    path = isolated_repo / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["parsing"]["ocr"].update(changes)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    reset_settings_cache()


def test_ac1_xbrl_every_numeric_fact_parsed_with_period_unit_decimals(kit) -> None:
    parsed = parse_xbrl(kit.xbrl.build_instance(), "standalone")
    expected = kit.xbrl.Expected()
    assert len(parsed.facts) == expected.numeric + expected.nil
    revenue = next(
        f for f in parsed.facts if f.source_loc == "OneD in-capmkt:RevenueFromOperations"
    )
    assert (revenue.period_start, revenue.period_end) == (date(2026, 1, 1), date(2026, 3, 31))
    assert (revenue.unit_raw, revenue.decimals, revenue.scale) == ("iso4217:INR", -4, 0)
    assert revenue.value == Decimal("7737020000")


def test_ac2_number_cleaning() -> None:
    assert parse_number("(1,234)") == Decimal("-1234")
    assert parse_number("1,23,456") == Decimal("123456")
    assert parse_number("—") is None
    assert parse_number("-") is None


def test_ac3_results_pdf_pl_page_classified_and_values_exactly_equal(kit) -> None:
    data = kit.results.build_pdf()
    pages = read_pdf(data)
    patterns = load_section_patterns()
    labels = classify_pages(pages, patterns)
    assert labels[0].statement_type == "profit_and_loss"
    result = extract_statement_tables(data, pages, labels, patterns, Basis.STANDALONE)
    for label, printed in kit.results.ROWS:
        facts = sorted(
            (f for f in result.facts if f.label_raw == label),
            key=lambda f: int(f.source_loc.split(":")[2]),
        )
        got = [f.value for f in facts]
        want = [parse_number(text) for text in printed]
        assert got == want, label


def test_ac4_unit_line_crore_gives_scale_ten_million(kit) -> None:
    assert unit_hint_of("(₹ in crore)", load_section_patterns()) == "crore"
    assert get_settings().units.multipliers["crore"] == 10_000_000
    data = kit.results.build_pdf("(Rs. in crore)")
    pages = read_pdf(data)
    patterns = load_section_patterns()
    result = extract_statement_tables(
        data, pages, classify_pages(pages, patterns), patterns, Basis.STANDALONE
    )
    assert {f.scale for f in result.facts} == {10_000_000}


def test_ac5_page_with_no_text_takes_the_ocr_path_for_both_backends(kit) -> None:
    for name in ("tesseract", "chandra"):
        ocr = kit.FakeOcr([kit.ocr_result(CLEAN, backend=name)], name=name)
        (page,) = read_pdf(kit.blank_pdf(), ocr)
        assert (page.method, page.ocr_backend) == ("ocr", name)
        assert len(ocr.images) == 1


def test_ac5a_backend_selection_and_shared_quality_check(kit, isolated_repo) -> None:
    assert get_backend().name == "tesseract"
    set_ocr(isolated_repo, backend="chandra")
    assert get_backend().name == "chandra"
    for name in ("tesseract", "chandra"):
        ocr = kit.FakeOcr([kit.ocr_result(GARBLED, backend=name)], name=name)
        (page,) = read_pdf(kit.blank_pdf(), ocr)
        assert page.low_quality is True and page.ocr_backend == name


def test_ac5b_chandra_downscales_and_handles_unavailable(kit, isolated_repo) -> None:
    big = io.BytesIO()
    Image.new("RGB", (4000, 3000), "white").save(big, format="PNG")
    limit = get_settings().parsing.ocr.chandra.max_image_px
    assert max(Image.open(io.BytesIO(downscale(big.getvalue(), limit))).size) <= limit
    with pytest.raises(OcrUnavailable):  # blank model name
        ChandraBackend().ocr_page(big.getvalue())
    set_ocr(
        isolated_repo, chandra={**get_settings().parsing.ocr.chandra.model_dump(), "model": "m"}
    )

    def down(url, body, timeout):
        raise OcrUnavailable("connection refused")

    with pytest.raises(OcrUnavailable, match="connection refused"):
        ChandraBackend(post=down).ocr_page(big.getvalue())
    (page,) = read_pdf(kit.blank_pdf(), ChandraBackend(post=down))
    assert page.ocr_error and not page.text  # flagged, not silently skipped


@pytest.mark.xfail(
    strict=True,
    reason="TODO: the Chandra response parser waits for a sample model response (user to provide)",
)
def test_ac5b_chandra_markdown_table_is_parsed_into_cells(isolated_repo) -> None:
    set_ocr(
        isolated_repo, chandra={**get_settings().parsing.ocr.chandra.model_dump(), "model": "m"}
    )
    answer = {"choices": [{"message": {"content": "| a | b |\n|---|---|\n| 1 | 2 |"}}]}
    image = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(image, format="PNG")
    result = ChandraBackend(post=lambda *a: answer).ocr_page(image.getvalue())
    assert result.tables == ((("a", "b"), ("1", "2")),)


def test_ac5c_ocr_facts_are_labelled_and_never_replace_xbrl(kit, register) -> None:
    xbrl_id = register(kit.xbrl.build_instance())
    run_parse()
    with transaction() as conn:
        before = conn.execute(
            sa.select(tables.parsed_facts.c.id, tables.parsed_facts.c.value).where(
                tables.parsed_facts.c.filing_id == xbrl_id
            )
        ).all()
    import pdfplumber

    from finrag.parsing.pdf_tables import _native_words

    table_pdf = kit.results.build_pdf()
    with pdfplumber.open(io.BytesIO(table_pdf)) as doc:
        words = tuple(
            kit.OcrWord(w.text, w.x0, w.x1, w.top, w.bottom) for w in _native_words(doc.pages[0])
        )
    import pymupdf

    text = pymupdf.open(stream=table_pdf, filetype="pdf")[0].get_text()
    scan_id = register(kit.blank_pdf(), fmt="pdf")
    run_parse(backend=kit.FakeOcr([kit.ocr_result(text, words)]))
    with transaction() as conn:
        pdf_rows = conn.execute(
            sa.select(tables.parsed_facts).where(tables.parsed_facts.c.filing_id == scan_id)
        ).all()
        after = conn.execute(
            sa.select(tables.parsed_facts.c.id, tables.parsed_facts.c.value).where(
                tables.parsed_facts.c.filing_id == xbrl_id
            )
        ).all()
    assert pdf_rows and {(r.extractor, r.ocr_backend) for r in pdf_rows} == {("ocr", "tesseract")}
    assert after == before


def test_ac6_low_confidence_table_goes_to_review_and_facts_are_kept(kit, register) -> None:
    filing_id = register(kit.results.build_pdf(kind_labels=False), fmt="pdf")
    run_parse(backend=kit.FakeOcr([]))
    with transaction() as conn:
        facts = conn.execute(
            sa.select(tables.parsed_facts).where(tables.parsed_facts.c.filing_id == filing_id)
        ).all()
        items = conn.execute(
            sa.select(tables.review_queue).where(
                tables.review_queue.c.item_type == "low_confidence_table"
            )
        ).all()
    assert len(items) == 1 and items[0].status == "open"
    assert facts and all(
        f.confidence < get_settings().parsing.pdf.min_table_confidence for f in facts
    )
