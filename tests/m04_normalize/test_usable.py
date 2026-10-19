"""PDF/OCR review requests are queued only if their value would be used (M04)."""

from __future__ import annotations

import json
from datetime import date

import sqlalchemy as sa

from finrag.core.db import tables, transaction
from finrag.core.types import FilingStatus
from finrag.ingestion import registry
from finrag.normalize import review_queue
from finrag.normalize.runner import run_normalize

Q4 = (date(2026, 1, 1), date(2026, 3, 31))


def pdf_fact(kit, value: str, label: str, confidence: float = 0.95, statement=None, **extra):
    """A PDF fact of the PDF filing (scale: millions, so 77.37 is 77,370,000 rupees)."""
    return kit.add_fact(
        value, label=label, unit=None, scale=1_000_000, extractor="pdf_table",
        confidence=confidence, filing_id=kit.PDF_FILING, statement=statement, **extra,
    )  # fmt: skip


def items(kind: str = "unmapped_concept") -> list[sa.Row]:
    return review_queue.list_items(None, kind)


def test_a_label_whose_value_xbrl_already_holds_is_not_queued(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("77370000", "in-capmkt:RevenueFromOperations")  # some XBRL fact of that value
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    pdf_fact(kit, "77.37", "Mystery Line Item", statement="profit_and_loss")
    (report,) = [
        r for r in run_normalize(writer, taxonomy=taxonomy) if r.filing_id == kit.PDF_FILING
    ]
    assert items() == []
    assert report.stats["not_queued"] == {"unmapped: xbrl_covers_key": 1}


def test_a_confident_uncovered_label_in_a_clean_column_is_queued(kit, taxonomy, writer) -> None:
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    pdf_fact(kit, "77.37", "Mystery Line Item", statement="profit_and_loss")
    run_normalize(writer, taxonomy=taxonomy)
    (item,) = items()
    assert json.loads(item.payload_json)["key"] == "mystery line item"


def test_a_label_below_the_minimum_confidence_is_not_queued(kit, taxonomy, writer) -> None:
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    pdf_fact(kit, "77.37", "Mystery Line Item", confidence=0.4, statement="profit_and_loss")
    (report,) = run_normalize(writer, taxonomy=taxonomy)
    assert items() == [] and report.stats["not_queued"] == {"unmapped: low_confidence": 1}


def test_a_label_in_a_column_that_fails_the_gate_identities_is_not_queued(
    kit, taxonomy, writer
) -> None:
    """Profit 304.79 is printed 30479 (100x): the column is refused, so nothing in it is used."""
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    for label, value in (
        ("Profit before tax", "416.06"),
        ("Total tax expense", "111.27"),
        ("Profit for the period", "30479"),
        ("Mystery Line Item", "5.55"),
    ):
        pdf_fact(kit, value, label, statement="profit_and_loss")
    (report,) = run_normalize(writer, taxonomy=taxonomy)
    got = {c.concept for c in writer.all}
    assert {"profit_before_tax", "tax_expense", "profit_after_tax"} <= got
    assert items() == [] and report.stats["not_queued"] == {"unmapped: column_fails_gate": 1}


def test_a_closed_item_is_reevaluated_and_queued_again_when_its_value_is_needed(
    kit, taxonomy, writer
) -> None:
    kit.add_filing()
    xbrl = kit.add_fact("77370000", "in-capmkt:RevenueFromOperations")
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    pdf_fact(kit, "77.37", "Mystery Line Item", statement="profit_and_loss")
    with transaction() as conn:  # an item for the label is already open (from an earlier run)
        review_queue.queue_unmapped(
            conn,
            kit.ISIN,
            "",
            "mystery line item",
            "label",
            kit.PDF_FILING,
            None,
            {},
            "profit_and_loss",
        )
    registry.set_status(kit.PDF_FILING, FilingStatus.PARSED)
    run_normalize(writer, taxonomy=taxonomy)
    (closed,) = items()
    assert closed.status == "ignored"
    assert json.loads(closed.resolution_json) == {
        "auto_closed": True,
        "reason": "XBRL covers this key",
    }
    with transaction() as conn:  # the XBRL fact goes away: the value is needed again
        conn.execute(sa.delete(tables.parsed_facts).where(tables.parsed_facts.c.id == xbrl))
    registry.set_status(kit.PDF_FILING, FilingStatus.PARSED)
    run_normalize(writer, taxonomy=taxonomy)
    assert [i.status for i in items()] == ["ignored", "open"]


def test_a_label_still_wanted_by_another_filing_keeps_its_item_open(kit, taxonomy, writer) -> None:
    """Covered in one PDF, uncovered in another: the item stays open."""
    kit.add_filing()
    kit.add_fact("77370000", "in-capmkt:RevenueFromOperations")
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    pdf_fact(kit, "77.37", "Mystery Line Item", statement="profit_and_loss")
    other = "FIL-dddddddddddd"
    kit.add_filing(other, fmt="pdf", family=None)
    kit.add_fact(
        "12.34", label="Mystery Line Item", unit=None, scale=1_000_000, extractor="pdf_table",
        confidence=0.95, filing_id=other, statement="profit_and_loss",
    )  # fmt: skip
    with transaction() as conn:
        review_queue.queue_unmapped(
            conn, kit.ISIN, "", "mystery line item", "label", other, None, {}, "profit_and_loss"
        )
    run_normalize(writer, taxonomy=taxonomy)
    assert [i.status for i in items()] == ["open"]


def test_unusable_pdf_cells_are_counted_not_queued_but_xbrl_issues_still_are(
    kit, taxonomy, writer
) -> None:
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    pdf_fact(kit, "1.5", "Revenue from operations", start=date(2024, 7, 1), end=date(2025, 6, 30))
    (pdf_report,) = [
        r for r in run_normalize(writer, taxonomy=taxonomy) if r.filing_id == kit.PDF_FILING
    ]
    assert pdf_report.stats["not_queued"] == {"period_misaligned: unusable_cell": 1}
    reasons = {json.loads(i.payload_json)["filing_id"] for i in items("normalization_issue")}
    assert reasons == set()


def test_a_duplicate_pdf_key_that_xbrl_covers_is_not_queued(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("77370000", "in-capmkt:RevenueFromOperations", start=Q4[0], end=Q4[1])
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    pdf_fact(kit, "77.37", "Revenue from operations", statement="profit_and_loss")
    pdf_fact(kit, "88.00", "Net sales", statement="profit_and_loss")  # a second label, same concept
    reports = {r.filing_id: r for r in run_normalize(writer, taxonomy=taxonomy)}
    assert items("normalization_issue") == []
    assert reports[kit.PDF_FILING].stats["not_queued"] == {
        "duplicate_candidates: xbrl_covers_key": 1
    }


def test_a_label_with_the_rows_numbers_merged_into_it_is_not_queued(kit, taxonomy, writer) -> None:
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    pdf_fact(kit, "566.22", "Property plant and equipment 566.22 736.64", statement="balance_sheet")
    pdf_fact(
        kit, "5.5", "Mystery Line Item (5-6)", statement="profit_and_loss"
    )  # numbering is fine
    (report,) = run_normalize(writer, taxonomy=taxonomy)
    assert [json.loads(i.payload_json)["key"] for i in items()] == ["mystery line item"]
    assert report.stats["not_queued"] == {"unmapped: garbled_label": 1}


def test_a_duplicate_pdf_key_that_no_xbrl_covers_is_counted_not_queued(
    kit, taxonomy, writer
) -> None:
    """Two pages of one PDF carry the same key and basis with different values (standalone and
    consolidated printed under one label): both are dropped, so nothing is used."""
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    pdf_fact(kit, "77.37", "Revenue from operations", statement="profit_and_loss")
    pdf_fact(kit, "88.00", "Net sales", statement="profit_and_loss")
    (report,) = run_normalize(writer, taxonomy=taxonomy)
    assert writer.all == [] and items("normalization_issue") == []
    assert report.stats["not_queued"] == {"duplicate_candidates: ambiguous_duplicate": 1}
