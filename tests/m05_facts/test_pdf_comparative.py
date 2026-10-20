"""A native-text PDF column of an earlier period than its filing's own is a comparative.

It is a second printing of a number, by a later filing: it never beats the number printed by the
filing of its own period (XBRL or PDF) and only fills a gap. A scanned column is never one.
"""

from __future__ import annotations

from finrag.core.types import Period, PeriodType
from finrag.facts.current_view import get_fact, history

ISIN = "INE111A01010"
BASIS = "consolidated"


def q1(kit) -> Period:
    s, e, k = kit.FY26["Q1"]
    return Period(s, e, PeriodType(k))


def versions(kit):
    return {f.source_filing_id: f for f in history(ISIN, "revenue", q1(kit), BASIS)}


def test_only_a_native_pdf_column_ending_before_its_filing_is_a_comparative(kit) -> None:
    kit.add_filing("FIL-xbrl", fmt="xbrl", period_end="2025-06-30")
    kit.add_filing("FIL-own", fmt="pdf", period_end="2025-06-30", filed_on="2025-07-30")
    kit.add_filing("FIL-later", fmt="pdf", period_end="2025-09-30", filed_on="2025-10-30")
    kit.add_filing("FIL-scan", fmt="pdf", period_end="2025-09-30", filed_on="2025-10-31")
    kit.write(
        [
            kit.cand("FIL-xbrl", "Q1", "100"),
            kit.cand("FIL-own", "Q1", "100", extractor="pdf_table", filed_on="2025-07-30"),
            kit.cand("FIL-later", "Q1", "120", extractor="pdf_table", filed_on="2025-10-30"),
            kit.cand("FIL-scan", "Q1", "130", extractor="ocr", filed_on="2025-10-31"),
        ]
    )
    found = versions(kit)
    assert {k: v.is_pdf_comparative for k, v in found.items()} == {
        "FIL-xbrl": False,  # XBRL is what the company filed for the period
        "FIL-own": False,  # the filing's own period
        "FIL-later": True,  # Q1 printed again by the Q2 results
        "FIL-scan": False,  # a scan has no restated comparatives: its digits are too uncertain
    }


def test_a_column_ending_with_the_filings_own_period_is_not_a_comparative(kit) -> None:
    """The half year printed in the Q2 results is the company's current report."""
    kit.add_filing("FIL-q2", fmt="pdf", period_end="2025-09-30", filed_on="2025-10-30")
    s, e, k = kit.FY26["H1"]
    kit.write([kit.cand("FIL-q2", "H1", "250", extractor="pdf_table", filed_on="2025-10-30")])
    (fact,) = history(ISIN, "revenue", Period(s, e, PeriodType(k)), BASIS)
    assert fact.filing_period_end == "2025-09-30" and not fact.is_pdf_comparative


def test_a_comparative_never_beats_the_original_in_xbrl(kit) -> None:
    kit.add_filing("FIL-xbrl", fmt="xbrl", period_end="2025-06-30")
    kit.add_filing("FIL-later", fmt="pdf", period_end="2025-09-30", filed_on="2025-10-30")
    kit.write([kit.cand("FIL-xbrl", "Q1", "100")])
    kit.write([kit.cand("FIL-later", "Q1", "120", extractor="pdf_table", filed_on="2025-10-30")])
    assert get_fact(ISIN, "revenue", q1(kit), BASIS).value == kit.D("100")


def test_a_comparative_never_beats_the_original_in_the_pdf_of_its_own_period(kit) -> None:
    """A later PDF used to win on filing date alone; the original is what was reported."""
    kit.add_filing("FIL-own", fmt="pdf", period_end="2025-06-30", filed_on="2025-07-30")
    kit.add_filing("FIL-later", fmt="pdf", period_end="2025-09-30", filed_on="2025-10-30")
    kit.write([kit.cand("FIL-own", "Q1", "100", extractor="pdf_table", filed_on="2025-07-30")])
    kit.write([kit.cand("FIL-later", "Q1", "120", extractor="pdf_table", filed_on="2025-10-30")])
    current = get_fact(ISIN, "revenue", q1(kit), BASIS)
    assert (current.value, current.source_filing_id) == (kit.D("100"), "FIL-own")


def test_a_comparative_fills_a_gap_and_beats_a_scan(kit) -> None:
    kit.add_filing("FIL-scan", fmt="pdf", period_end="2025-06-30", filed_on="2025-07-30")
    kit.add_filing("FIL-later", fmt="pdf", period_end="2025-09-30", filed_on="2025-10-30")
    kit.write([kit.cand("FIL-scan", "Q1", "100", extractor="ocr", filed_on="2025-07-30")])
    assert get_fact(ISIN, "revenue", q1(kit), BASIS).value == kit.D("100")
    kit.write([kit.cand("FIL-later", "Q1", "120", extractor="pdf_table", filed_on="2025-10-30")])
    current = get_fact(ISIN, "revenue", q1(kit), BASIS)
    assert (current.value, current.source_filing_id) == (kit.D("120"), "FIL-later")
