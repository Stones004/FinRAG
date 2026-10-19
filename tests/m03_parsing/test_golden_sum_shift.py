"""The real Netweb Q2 FY26 results scan: the Q1 FY26 profit misread as 30479 is caught.

Reads the stored page text and OCR word boxes (no OCR runs) and the raw PDF; writes nothing.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa

from finrag.core.config import get_settings
from finrag.core.types import Basis
from finrag.parsing.ocr.base import OcrWord
from finrag.parsing.page_classifier import classify_pages
from finrag.parsing.pdf_tables import extract_statement_tables
from finrag.parsing.pdf_text import PageText
from finrag.parsing.sections import load_section_patterns

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data" / "finrag.db"
FILING = "FIL-adc5c2c447ce"  # Netweb, results for the quarter and half year ended 30 Sep 2025
PAGES = REPO / "data" / "processed" / FILING / "pages.jsonl"


def _raw_path() -> Path | None:
    if not DB.exists() or not PAGES.exists():
        return None
    engine = sa.create_engine(f"sqlite:///file:{DB.as_posix()}?mode=ro&uri=true")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("select raw_path from filings where filing_id = :f"), {"f": FILING}
            ).first()
    except sa.exc.OperationalError:
        return None
    finally:
        engine.dispose()
    return Path(row[0]) if row and Path(row[0]).exists() else None


RAW = _raw_path()
pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(RAW is None, reason="the real Netweb Q2 FY26 PDF is not on this machine"),
]


def _pages() -> list[PageText]:
    out = []
    for line in PAGES.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        words = tuple(OcrWord(w[0], w[1], w[2], w[3], w[4]) for w in r.get("words", []))
        out.append(
            PageText(
                r["page"], r["text"], r["method"], r.get("ocr_backend"), r["char_count"],
                r["quality"], r["low_quality"], words=words,
            )
        )  # fmt: skip
    return out


def test_q1_fy26_profit_misread_as_30479_is_flagged_by_the_sum_check() -> None:
    pages = _pages()
    patterns = load_section_patterns()
    labels = classify_pages(pages, patterns)
    result = extract_statement_tables(
        RAW.read_bytes(), pages, labels, patterns, Basis.STANDALONE, get_settings()
    )
    cap = get_settings().parsing.pdf.sum_shift_confidence
    profit = {
        (f.period_start, f.period_end): f
        for f in result.facts
        if f.label_raw == "Profit for the period / year (7-8)"
    }
    q1 = profit[(date(2025, 4, 1), date(2025, 6, 30))]
    assert str(q1.value) == "30479" and q1.confidence <= cap
    for key, fact in profit.items():  # every other column of the row keeps its normal confidence
        if key != (date(2025, 4, 1), date(2025, 6, 30)):
            assert fact.confidence > cap
    flagged = [item for page in result.review_items for item in page.get("sum_shifted", [])]
    assert [item["label"] for item in flagged] == ["Profit for the period / year (7-8)"]
