"""Golden checks against real filings (skipped when tests/golden or the filings are absent).

1. Totals parsed from real XBRL files equal the values in golden_values.yaml, using only entries
   marked `verified: true` (checked by hand against the companies' results PDFs).
2. Netweb Q4 FY26, non-circular: the XBRL revision and the scanned results PDF (read by real OCR)
   must agree on revenue, profit, total assets and operating cash flow within rounding. Skipped
   when Tesseract is not installed.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from finrag.core.config import get_settings
from finrag.core.types import Basis
from finrag.parsing.ocr.tesseract import TesseractBackend
from finrag.parsing.page_classifier import classify_pages
from finrag.parsing.pdf_tables import extract_statement_tables
from finrag.parsing.pdf_text import extract_pdf_text
from finrag.parsing.sections import load_section_patterns
from finrag.parsing.text_quality import load_vocabulary
from finrag.parsing.xbrl_parser import parse_xbrl

REAL = Path(__file__).resolve().parents[2]
GOLDEN = REAL / "tests" / "golden" / "golden_values.yaml"
HANDLED = REAL / "data" / "inbox" / "_processed"
NETWEB = "INE0NT901020"
NETWEB_XBRL = f"{NETWEB}__financial_results__Q4FY26__standalone__rev1.xml"
NETWEB_PDF = f"{NETWEB}__financial_results__Q4FY26__standalone.pdf"

pytestmark = pytest.mark.golden


def verified_entries() -> list[tuple[str, dict]]:
    """(isin, entry) for every golden entry with verified: true."""
    if not GOLDEN.exists():
        return []
    data = yaml.safe_load(GOLDEN.read_text(encoding="utf-8")) or {}
    return [
        (isin, entry)
        for isin, entries in data.items()
        if isinstance(entries, list)
        for entry in entries
        if entry.get("verified") is True
    ]


ENTRIES = verified_entries()


@pytest.mark.skipif(not ENTRIES, reason="no verified golden values (tests/golden is empty)")
@pytest.mark.parametrize(
    ("isin", "entry"), ENTRIES, ids=[f"{i}-{e['concept']}-{e['period']}" for i, e in ENTRIES]
)
def test_xbrl_totals_match_verified_golden_values(isin: str, entry: dict) -> None:
    source = HANDLED / entry["source_file"]
    if not source.exists():
        pytest.skip(f"{source.name} is not in data/inbox/_processed")
    parsed = parse_xbrl(source.read_bytes(), entry["basis"])
    dates = [d if isinstance(d, date) else date.fromisoformat(d) for d in entry["period_dates"]]
    start, end = dates[0], dates[-1]
    matches = [
        f
        for f in parsed.facts
        if f.concept_raw == entry["xbrl_concept"]
        and (f.period_start, f.period_end) == (start, end)
        and not f.dimensions
    ]
    assert len(matches) == 1, f"{entry['xbrl_concept']} {start}..{end}: {len(matches)} facts"
    assert matches[0].value == Decimal(entry["value_inr"])


def tesseract_available() -> bool:
    """True if a real Tesseract can be run with the configured/PATH command."""
    try:
        import pytesseract
    except ImportError:
        return False
    cmd = get_settings().parsing.ocr.tesseract_cmd
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        pytesseract.get_tesseract_version()
    except Exception:  # pytesseract raises TesseractNotFoundError and others
        return False
    return True


def find(facts, pattern: str, start: date | None, end: date):
    """The single fact whose label matches and whose period is (start, end)."""
    rx = re.compile(pattern, re.IGNORECASE)
    hits = [
        f
        for f in facts
        if f.label_raw and rx.search(f.label_raw) and (f.period_start, f.period_end) == (start, end)
    ]
    assert len(hits) == 1, f"{pattern!r} {start}..{end}: {[(h.label_raw, h.value) for h in hits]}"
    return hits[0]


@pytest.mark.skipif(not (HANDLED / NETWEB_PDF).exists(), reason="Netweb Q4 FY26 PDF not found")
@pytest.mark.skipif(not tesseract_available(), reason="Tesseract (OCR) is not installed")
def test_netweb_q4_fy26_pdf_agrees_with_xbrl_revision() -> None:
    xbrl = parse_xbrl((HANDLED / NETWEB_XBRL).read_bytes(), Basis.STANDALONE)
    pdf = (HANDLED / NETWEB_PDF).read_bytes()
    settings = get_settings()
    pages = extract_pdf_text(
        pdf, "financial_results", TesseractBackend(settings), load_vocabulary(), settings
    ).pages
    patterns = load_section_patterns()
    labels = classify_pages(pages, patterns)
    result = extract_statement_tables(pdf, pages, labels, patterns, Basis.STANDALONE)
    assert result.facts, f"no facts read from the PDF: {result.pages_unread}"
    assert {f.extractor for f in result.facts} == {"ocr"}
    quarter, year = (date(2026, 1, 1), date(2026, 3, 31)), (date(2025, 4, 1), date(2026, 3, 31))
    checks = [
        ("in-capmkt:RevenueFromOperations", r"revenue from operations", *quarter),
        ("in-capmkt:ProfitLossForPeriod", r"profit for the (period|year)", *quarter),
        ("in-capmkt:Assets", r"total assets", date(2026, 3, 31), date(2026, 3, 31)),
        (
            "in-capmkt:CashFlowsFromUsedInOperatingActivities",
            r"net cash .*operating activities",
            *year,
        ),
    ]
    for concept, label, start, end in checks:
        reported = next(
            f
            for f in xbrl.facts
            if f.concept_raw == concept
            and (f.period_start, f.period_end) == (start, end)
            and not f.dimensions
        )
        printed = find(result.facts, label, start, end)
        assert printed.scale == 1_000_000  # "Indian Rupees in millions"
        in_rupees = printed.value * printed.scale
        # XBRL is rounded to 10**-decimals rupees; the PDF prints millions to 2 decimals
        tolerance = Decimal(10) ** (-reported.decimals) / 2 + Decimal("0.005") * printed.scale
        assert abs(in_rupees - reported.value) <= tolerance, (concept, in_rupees, reported.value)
