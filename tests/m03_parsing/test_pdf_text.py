"""Page text extraction: native layer, OCR routing, quality flags, pages.jsonl."""

from __future__ import annotations

import json
import textwrap

import pymupdf
import pytest

from finrag.core.config import get_settings
from finrag.parsing.pdf_text import (
    PdfReadError,
    extract_pdf_text,
    read_pages_jsonl,
    write_pages_jsonl,
)
from finrag.parsing.text_quality import load_vocabulary

CLEAN = (
    "Revenue from operations increased because the company sold more products to customers "
    "during the quarter. Management expects demand to remain strong across the business and "
    "believes that margins will improve as costs come down over the next few quarters."
)
GARBLED = (
    "Revmue fom operatons Prcfit befor tsx Finmce costs Deproiation mortietion expenss "
    "Cost of matenals consumd Employe benefts expens Other expenres Total expens6 Profit"
    " for the perod ycar Paid up eqrity shrre capiasl Other eqity Partictrlnn Audied"
)


@pytest.fixture
def vocabulary():
    return load_vocabulary()


def run(kit, vocabulary, data, backend=None, filing_type="annual_report"):
    return extract_pdf_text(data, filing_type, backend, vocabulary)


def test_clean_native_page_needs_no_ocr(kit, vocabulary) -> None:
    ocr = kit.FakeOcr([])
    result = run(kit, vocabulary, kit.text_pdf([CLEAN]), ocr)
    page = result.pages[0]
    assert (page.method, page.ocr_backend, page.low_quality) == ("native", None, False)
    assert page.char_count == len(page.text.strip())
    assert ocr.images == []


def test_page_with_no_text_goes_to_ocr(kit, vocabulary) -> None:
    ocr = kit.FakeOcr([kit.ocr_result(CLEAN)])
    result = run(kit, vocabulary, kit.blank_pdf(), ocr)
    page = result.pages[0]
    assert (page.method, page.ocr_backend, page.ocr_reason) == ("ocr", "tesseract", "too_short")
    assert page.text == CLEAN
    assert len(ocr.images) == 1
    assert ocr.images[0].startswith(b"\x89PNG")  # a rendered page image went to the backend


def test_poor_text_layer_goes_to_ocr_even_when_long(kit, vocabulary) -> None:
    ocr = kit.FakeOcr([kit.ocr_result(CLEAN)])
    result = run(kit, vocabulary, kit.text_pdf([GARBLED]), ocr)
    page = result.pages[0]
    assert page.ocr_reason == "low_quality"
    assert (page.method, page.text) == ("ocr", CLEAN)
    assert page.native_quality is not None
    assert page.native_quality < get_settings().parsing.pdf.min_text_quality


def test_ocr_is_skipped_when_disabled(kit, vocabulary, isolated_repo) -> None:
    import yaml

    from finrag.core.config import reset_settings_cache

    path = isolated_repo / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["parsing"]["ocr"]["ocr_enabled"] = False
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    reset_settings_cache()
    ocr = kit.FakeOcr([])
    page = run(kit, vocabulary, kit.blank_pdf(), ocr).pages[0]
    assert (page.method, page.ocr_error) == ("native", "OCR disabled or no backend")
    assert ocr.images == []


def test_ocr_output_goes_through_the_same_quality_check(kit, vocabulary) -> None:
    ocr = kit.FakeOcr([kit.ocr_result(GARBLED)])
    page = run(kit, vocabulary, kit.blank_pdf(), ocr).pages[0]
    assert page.method == "ocr"  # better than nothing
    assert page.low_quality is True  # but flagged, never silently accepted
    assert page.quality < get_settings().parsing.pdf.min_text_quality


def test_chandra_output_is_checked_like_tesseract(kit, vocabulary) -> None:
    ocr = kit.FakeOcr([kit.ocr_result(GARBLED, backend="chandra")], name="chandra")
    page = run(kit, vocabulary, kit.blank_pdf(), ocr).pages[0]
    assert (page.ocr_backend, page.low_quality) == ("chandra", True)


def test_better_native_text_is_kept_over_worse_ocr(kit, vocabulary) -> None:
    ocr = kit.FakeOcr([kit.ocr_result("zzz qqq xxx")])
    page = run(kit, vocabulary, kit.text_pdf([GARBLED]), ocr).pages[0]
    assert page.method == "native"
    assert page.low_quality is True


def test_unavailable_ocr_flags_the_page_and_is_not_retried(kit, vocabulary) -> None:
    ocr = kit.FakeOcr([kit.OcrUnavailable("tesseract is not installed")])
    result = run(kit, vocabulary, kit.text_pdf([GARBLED, GARBLED, CLEAN]), ocr)
    assert [p.ocr_error for p in result.pages] == [
        "tesseract is not installed",
        "tesseract is not installed",
        None,
    ]
    assert len(ocr.images) == 1  # the second bad page did not call the backend again
    assert result.pages[0].low_quality and result.pages[1].low_quality
    assert result.stats()["ocr_failed_pages"] == [1, 2]
    assert result.stats()["low_quality_pages"] == [1, 2]


def test_blank_page_is_counted_not_flagged(kit, vocabulary) -> None:
    ocr = kit.FakeOcr([kit.ocr_result("")])
    result = run(kit, vocabulary, kit.blank_pdf(), ocr)
    assert result.pages[0].low_quality is False
    assert result.stats()["blank_pages"] == 1


def test_stats_counts(kit, vocabulary) -> None:
    ocr = kit.FakeOcr([kit.ocr_result(CLEAN)])
    result = run(kit, vocabulary, kit.text_pdf([CLEAN, ""]), ocr)
    stats = result.stats()
    assert (stats["pages"], stats["native_pages"], stats["ocr_pages"]) == (2, 1, 1)


def test_transcript_pages_carry_line_font_info(kit, vocabulary) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Moderator:", fontname="hebo", fontsize=10)
    page.insert_text(
        (120, 60), "Welcome to the call and thank you for joining us today.", fontsize=10
    )
    page.insert_text(
        (50, 80), "Management: the quarter went well for the business overall.", fontsize=10
    )
    for i, text in enumerate(textwrap.wrap(CLEAN, 80)):  # enough clean text to need no OCR
        page.insert_text((50, 100 + 14 * i), text, fontsize=10)
    data = doc.tobytes()
    doc.close()
    result = run(
        kit, vocabulary, data, kit.FakeOcr([kit.ocr_result(CLEAN)]), "earnings_call_transcript"
    )
    lines = result.pages[0].lines
    assert lines is not None
    first = next(line for line in lines if line["text"].startswith("Moderator:"))
    assert first["bold"] is True
    assert {"text", "bold", "font", "size"} <= set(first)
    second = next(line for line in lines if line["text"].startswith("Management:"))
    assert second["bold"] is False


def test_lines_are_absent_for_other_filing_types_and_ocr_pages(kit, vocabulary) -> None:
    assert run(kit, vocabulary, kit.text_pdf([CLEAN])).pages[0].lines is None
    ocr = kit.FakeOcr([kit.ocr_result(CLEAN)])
    page = run(kit, vocabulary, kit.blank_pdf(), ocr, "earnings_call_transcript").pages[0]
    assert page.lines is None


def test_pages_jsonl_fields(kit, vocabulary, tmp_path) -> None:
    words = (kit.OcrWord("Revenue", 10.0, 50.0, 20.0, 30.0),)
    ocr = kit.FakeOcr([kit.ocr_result(CLEAN, words)])
    result = run(kit, vocabulary, kit.text_pdf([CLEAN, ""]), ocr)
    path = tmp_path / "out" / "pages.jsonl"
    write_pages_jsonl(path, result.pages)
    records = read_pages_jsonl(path)
    assert [r["page"] for r in records] == [1, 2]
    assert {"page", "text", "method", "ocr_backend", "char_count", "lines"} <= set(records[0])
    assert records[0]["method"] == "native" and records[0]["ocr_backend"] is None
    assert records[1]["method"] == "ocr" and records[1]["ocr_backend"] == "tesseract"
    assert records[1]["words"] == [["Revenue", 10.0, 50.0, 20.0, 30.0]]
    assert all(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())


def test_unreadable_pdf_raises(kit, vocabulary) -> None:
    with pytest.raises(PdfReadError):
        run(kit, vocabulary, b"this is not a pdf")
