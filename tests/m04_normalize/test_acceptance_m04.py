"""Acceptance criteria of docs/modules/M04_normalization.md, one test each."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from finrag.cli import app
from finrag.core.db import tables, transaction
from finrag.core.types import FilingStatus
from finrag.ingestion import registry
from finrag.normalize import review_queue
from finrag.normalize.runner import run_normalize
from finrag.normalize.taxonomy import TaxonomyError, parse_taxonomy

FY = (date(2025, 4, 1), date(2026, 3, 31))
runner = CliRunner()


def unmapped_items() -> list[sa.Row]:
    return review_queue.list_items(None, "unmapped_concept")


def test_1_known_xbrl_qname_maps_standard(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("12095930000", "in-capmkt:RevenueFromOperations")
    run_normalize(writer, taxonomy=taxonomy)
    (candidate,) = writer.all
    assert (candidate.concept, candidate.mapping_method) == ("revenue", "standard")


@pytest.mark.parametrize(
    "label",
    ["Revenue from Operations", "REVENUE FROM OPERATIONS", "  revenue   from   operations "],
)
def test_2_label_variants_map_label(kit, taxonomy, writer, label: str) -> None:
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    kit.add_fact(
        "77.37", label=label, unit=None, scale=1_000_000, extractor="pdf_table",
        confidence=0.95, filing_id=kit.PDF_FILING,
    )  # fmt: skip
    run_normalize(writer, taxonomy=taxonomy)
    (candidate,) = writer.all
    assert (candidate.concept, candidate.mapping_method) == ("revenue", "label")


def test_3_unknown_label_is_queued_once_and_occurrences_increase(kit, taxonomy, writer) -> None:
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    kit.add_fact(
        "1", label="Mystery Line Item", unit=None, scale=1_000_000, extractor="pdf_table",
        confidence=0.95, filing_id=kit.PDF_FILING,
    )  # fmt: skip
    run_normalize(writer, taxonomy=taxonomy)
    (item,) = unmapped_items()
    assert item.occurrences == 1 and item.status == "open"
    assert json.loads(item.payload_json)["key"] == "mystery line item"

    registry.set_status(kit.PDF_FILING, FilingStatus.PARSED)
    run_normalize(writer, taxonomy=taxonomy)
    (again,) = unmapped_items()  # no duplicate item
    assert again.id == item.id and again.occurrences == 2


def test_3b_fuzzy_match_is_a_suggestion_not_a_mapping(kit, taxonomy, writer) -> None:
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    kit.add_fact(
        "1", label="Revenue from operation", unit=None, scale=1_000_000, extractor="pdf_table",
        confidence=0.95, filing_id=kit.PDF_FILING,
    )  # fmt: skip
    run_normalize(writer, taxonomy=taxonomy)
    assert writer.all == []
    (item,) = unmapped_items()
    assert json.loads(item.payload_json)["suggestion"]["concept"] == "revenue"


def test_4_resolving_an_item_maps_it_manual_next_run(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("123", "in-capmkt:NeverSeenBefore")
    run_normalize(writer, taxonomy=taxonomy)
    assert writer.all == []
    (item,) = unmapped_items()

    review_queue.resolve(item.id, "revenue", taxonomy)
    registry.set_status(kit.FILING, FilingStatus.PARSED)
    run_normalize(writer, taxonomy=taxonomy)
    (candidate,) = writer.all
    assert (candidate.concept, candidate.mapping_method) == ("revenue", "manual")
    assert candidate.value == Decimal("123")
    (closed,) = unmapped_items()
    assert closed.status == "resolved" and closed.occurrences == 1  # not queued again


def test_4b_ignored_items_are_not_queued_again(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("123", "in-capmkt:NeverSeenBefore")
    run_normalize(writer, taxonomy=taxonomy)
    (item,) = unmapped_items()
    review_queue.ignore(item.id)
    registry.set_status(kit.FILING, FilingStatus.PARSED)
    (report,) = run_normalize(writer, taxonomy=taxonomy)
    assert report.stats["known_unmapped_by_reason"] == {"user_ignored": 1}
    assert len(unmapped_items()) == 1


def test_5_crore_scale_gives_exact_rupees(kit, taxonomy, writer) -> None:
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    kit.add_fact(
        "1234.56", label="Revenue from operations", unit=None, scale=10_000_000,
        extractor="pdf_table", confidence=0.95, filing_id=kit.PDF_FILING,
    )  # fmt: skip
    run_normalize(writer, taxonomy=taxonomy)
    (candidate,) = writer.all
    assert candidate.value == Decimal("12345600000")
    assert isinstance(candidate.value, Decimal)


def _pdf_revenue(kit, confidence: float) -> None:
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    kit.add_fact(
        "1234.56", label="Revenue from operations", start=FY[0], end=FY[1], unit=None,
        scale=1_000_000, extractor="ocr", confidence=confidence, filing_id=kit.PDF_FILING,
    )  # fmt: skip


def test_5a_xbrl_and_pdf_candidates_both_reach_the_writer_with_their_extractor(
    kit, taxonomy, writer
) -> None:
    kit.add_filing()
    kit.add_fact("1234560000", "in-capmkt:RevenueFromOperations", start=FY[0], end=FY[1])
    _pdf_revenue(kit, 0.95)
    run_normalize(writer, taxonomy=taxonomy)
    by_extractor = {c.extractor: c for c in writer.all}
    assert set(by_extractor) == {"xbrl", "ocr"}
    assert by_extractor["xbrl"].key == by_extractor["ocr"].key
    assert by_extractor["ocr"].confidence == 0.95


def test_5a_low_confidence_pdf_covered_by_xbrl_needs_no_item(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("1234560000", "in-capmkt:RevenueFromOperations", start=FY[0], end=FY[1])
    _pdf_revenue(kit, 0.3)
    run_normalize(writer, taxonomy=taxonomy)
    assert len(writer.all) == 2
    assert review_queue.list_items(None, "normalization_issue") == []


def test_5a_low_confidence_pdf_as_sole_source_is_counted_not_queued(kit, taxonomy, writer) -> None:
    """The gap-fill gate (M05) refuses the value and says so; M04 raises no item for it."""
    _pdf_revenue(kit, 0.3)
    (report,) = run_normalize(writer, taxonomy=taxonomy)
    (candidate,) = writer.all
    assert candidate.confidence == 0.3 and candidate.extractor == "ocr"
    assert review_queue.list_items(None, "normalization_issue") == []
    assert report.stats["not_queued"] == {"gate_owns_low_confidence": 1}


def test_5a_confident_pdf_sole_source_needs_no_item(kit, taxonomy, writer) -> None:
    _pdf_revenue(kit, 0.95)
    run_normalize(writer, taxonomy=taxonomy)
    assert len(writer.all) == 1
    assert review_queue.list_items(None, "normalization_issue") == []


def test_6_duplicate_qname_in_taxonomy_fails_to_load(kit) -> None:
    data = kit.taxonomy_data()
    data["concepts"]["profit_after_tax"]["xbrl_concepts"]["indas"].append(
        "in-capmkt:RevenueFromOperations"
    )
    with pytest.raises(TaxonomyError, match="duplicate mapping"):
        parse_taxonomy(data)


def test_7_report_shows_percent_mapped_by_statement_and_counts_by_reason(
    kit, taxonomy_file, writer
) -> None:
    kit.add_filing()
    kit.add_fact("100", "in-capmkt:RevenueFromOperations", start=FY[0], end=FY[1])  # P&L
    kit.add_fact("50", "in-capmkt:Assets", start=FY[1], end=FY[1])  # balance sheet
    kit.add_fact("10", "in-capmkt:CashFlowsFromOperating", start=FY[0], end=FY[1])  # cash flow
    kit.add_fact("5", "in-capmkt:AdjustmentsForWorkingCapital", start=FY[0], end=FY[1])
    kit.add_fact("7", "in-capmkt:NeverSeenBefore", start=FY[0], end=FY[1])
    result = runner.invoke(app, ["normalize", "report", "--isin", kit.ISIN])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any(line.startswith("profit_and_loss") and "20.0%" in line for line in lines)
    assert any(line.startswith("balance_sheet") and "20.0%" in line for line in lines)
    assert any(line.startswith("cash_flow") and "20.0%" in line for line in lines)
    assert any(line.startswith("all mapped") and "60.0%" in line for line in lines)
    assert any("cash_flow_adjustment" in line and line.rstrip().endswith("1") for line in lines)
    assert any("in-capmkt:NeverSeenBefore" in line for line in lines)
    # report is read-only: nothing queued, no status change
    assert review_queue.list_items(None) == []
    with transaction() as conn:
        status = conn.execute(sa.select(tables.filings.c.status)).scalar_one()
    assert status == "parsed"
