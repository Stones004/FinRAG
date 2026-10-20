"""A growth over a restated period: against the original base, and against the restated comparative.

The company restated an earlier period (a recorded `comparative_restatement` event) and the results
of the later period print that earlier period again, restated, in a native-text PDF. The headline
stays the figure against what was first filed; the figure against the restated comparative is
stored beside it with the event, the facts it rests on, and the flag that the headline is not like
for like. Nothing is stored unless an event explains the restatement.
"""

from __future__ import annotations

from decimal import Decimal

import sqlalchemy as sa

from finrag.core.db import tables, transaction
from finrag.metrics.engine import run_metrics
from finrag.metrics.query import get_metric

D = Decimal
FY26, FY25 = ("2025-04-01", "2026-03-31", "FY"), ("2024-04-01", "2025-03-31", "FY")
BASIS = "consolidated"
HEADLINE = D(1000) / D(800) - 1  # revenue 1000 against the 800 first filed for FY25
RESTATED = D(1000) / D(900) - 1  # against the 900 the FY26 results print for FY25


def growth(kit, basis: str = BASIS):
    return get_metric(kit.ISIN, "revenue_growth_yoy", kit.period(*FY26), basis)


def printed_again(kit, value: str = "900", src: str = "pdf") -> str:
    """The FY26 results (a PDF filing) print FY25 revenue again."""
    return kit.put(
        "revenue", FY25[0], FY25[1], value, "FY", src=src, filing="FIL-p", filed_on="2026-06-01"
    )


def restated_event(kit, **kw) -> int:
    details = {"pdf_filing_id": "FIL-p", **kw}
    return kit.event("comparative_restatement", "2025-04-01", citation="Scheme", **details)


def test_growth_is_stored_on_the_original_and_on_the_restated_comparative(kit) -> None:
    kit.load_acme()
    again = printed_again(kit)
    event = restated_event(kit)
    run_metrics(kit.ISIN)
    v = growth(kit)
    assert v.value == HEADLINE  # the headline is unchanged: against what was first filed
    found = v.details["like_for_like"]
    assert found["headline_like_for_like"] is False
    assert D(found["restated_value"]) == RESTATED
    assert found["event"]["event_id"] == event and found["event"]["citation"] == "Scheme"
    (item,) = found["inputs"]
    assert (item["alias"], item["concept"], item["period"]) == ("rev_ly", "revenue", "FY25")
    assert (D(item["original_value"]), D(item["restated_value"])) == (
        D(800) * 10**6,
        D(900) * 10**6,
    )
    assert item["restated_fact_id"] == again and item["restated_filing_id"] == "FIL-p"
    assert "Not like for like" in v.reason and f"event {event}" in v.reason


def test_the_restated_facts_are_not_the_current_view(kit) -> None:
    """The comparative is a second printing: the original is still the value of FY25."""
    kit.load_acme()
    printed_again(kit)
    restated_event(kit)
    run_metrics(kit.ISIN)
    assert get_metric(kit.ISIN, "revenue_growth_yoy", kit.period(*FY26), BASIS).value == HEADLINE
    with transaction() as conn:
        used = conn.execute(
            sa.select(tables.metric_values.c.input_fact_ids_json).where(
                tables.metric_values.c.metric_id == "revenue_growth_yoy"
            )
        ).first()
    assert "FIL-p" not in used[0]  # fact ids of the originals only


def test_nothing_is_stored_without_an_event_that_explains_the_comparative(kit) -> None:
    kit.load_acme()
    printed_again(kit)
    run_metrics(kit.ISIN)
    assert "like_for_like" not in growth(kit).details


def test_an_event_that_explains_another_filing_does_not_explain_this_one(kit) -> None:
    kit.load_acme()
    printed_again(kit)
    kit.event("comparative_restatement", "2025-04-01", pdf_filing_id="FIL-other", citation="Scheme")
    run_metrics(kit.ISIN)
    assert "like_for_like" not in growth(kit).details


def test_an_event_linked_to_the_filing_explains_it(kit) -> None:
    kit.load_acme()
    printed_again(kit)
    restated_event(kit, pdf_filing_id="FIL-first", linked_filings=[{"pdf_filing_id": "FIL-p"}])
    run_metrics(kit.ISIN)
    assert "like_for_like" in growth(kit).details


def test_an_event_of_the_other_basis_does_not_explain_it(kit) -> None:
    kit.load_acme()
    printed_again(kit)
    restated_event(kit, bases=["standalone"])
    run_metrics(kit.ISIN)
    assert "like_for_like" not in growth(kit).details


def test_an_event_listing_both_bases_explains_both(kit) -> None:
    kit.load_acme()
    printed_again(kit)
    restated_event(kit, bases=["standalone", "consolidated"])
    run_metrics(kit.ISIN)
    assert "like_for_like" in growth(kit).details


def test_a_comparative_equal_to_the_original_is_not_a_restatement(kit) -> None:
    kit.load_acme()
    printed_again(kit, "800.001")  # rounding of the printed figure
    restated_event(kit)
    run_metrics(kit.ISIN)
    assert "like_for_like" not in growth(kit).details


def test_a_scanned_comparative_is_never_used(kit) -> None:
    kit.load_acme()
    printed_again(kit, src="ocr")
    restated_event(kit)
    run_metrics(kit.ISIN)
    assert "like_for_like" not in growth(kit).details


def test_a_comparative_printed_with_other_results_is_not_the_base_of_this_growth(kit) -> None:
    """FY25 as printed in the FY27 results is not the comparative of the FY26 results."""
    kit.load_acme()
    printed_again(kit)
    restated_event(kit)
    with transaction() as conn:
        conn.execute(
            sa.update(tables.filings)
            .where(tables.filings.c.filing_id == "FIL-p")
            .values(period_end="2027-03-31")
        )
    run_metrics(kit.ISIN)
    assert "like_for_like" not in growth(kit).details
