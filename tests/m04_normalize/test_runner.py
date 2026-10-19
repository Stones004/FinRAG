"""The normalization runner: counts, review items, status changes and edge cases."""

from __future__ import annotations

import json
from datetime import date

import pytest
import sqlalchemy as sa

from finrag.core.db import tables, transaction
from finrag.core.types import FilingStatus
from finrag.ingestion import registry
from finrag.normalize.runner import NormalizeError, run_normalize

Q4 = (date(2026, 1, 1), date(2026, 3, 31))
FY = (date(2025, 4, 1), date(2026, 3, 31))


def items(item_type: str) -> list[sa.Row]:
    with transaction() as conn:
        return list(
            conn.execute(
                sa.select(tables.review_queue).where(tables.review_queue.c.item_type == item_type)
            )
        )


def filing_status(filing_id: str) -> str:
    with transaction() as conn:
        return conn.execute(
            sa.select(tables.filings.c.status).where(tables.filings.c.filing_id == filing_id)
        ).scalar_one()


def test_counts_candidates_and_status(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("12095930000", "in-capmkt:RevenueFromOperations")
    kit.add_fact(
        "999", "in-capmkt:RevenueFromOperations", dimensions=[{"axis": "a", "member": "m"}]
    )
    kit.add_fact(None, "in-capmkt:ProfitLossForPeriod")
    kit.add_fact("5", "in-capmkt:AdjustmentsForWorkingCapital")
    kit.add_fact("7", "in-capmkt:NeverSeenBefore")

    (report,) = run_normalize(writer, taxonomy=taxonomy)

    assert report.status == "normalized" and report.error is None
    assert filing_status(kit.FILING) == "normalized"
    (candidate,) = writer.all
    assert candidate.concept == "revenue" and candidate.value == kit.D("12095930000")
    assert (candidate.period_type, candidate.basis, candidate.unit) == ("Q", "consolidated", "INR")
    assert candidate.mapping_method == "standard" and candidate.extractor == "xbrl"
    assert candidate.isin == kit.ISIN and candidate.filing_id == kit.FILING
    assert candidate.filed_on == "2026-05-01" and candidate.confidence == 1.0
    stats = report.stats
    assert stats["dimensioned_skipped"] == 1 and stats["nil_skipped"] == 1
    assert stats["known_unmapped_by_reason"] == {"cash_flow_adjustment": 1}
    assert stats["unmapped"] == 1 and stats["mapped_by_statement"] == {"profit_and_loss": 1}
    with transaction() as conn:
        job = conn.execute(sa.select(tables.jobs)).one()
    assert job.job_type == "normalize" and job.status == "succeeded"
    assert json.loads(job.stats_json)["candidates"] == 1


def test_only_unmapped_qnames_are_queued(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("5", "in-capmkt:AdjustmentsForWorkingCapital")
    kit.add_fact("7", "in-capmkt:NeverSeenBefore")
    run_normalize(writer, taxonomy=taxonomy)
    (item,) = items("unmapped_concept")
    payload = json.loads(item.payload_json)
    assert payload["key"] == "in-capmkt:NeverSeenBefore" and payload["family"] == "indas"
    assert payload["isin"] == kit.ISIN and payload["key_type"] == "qname"


def test_opening_cash_is_a_separate_concept(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("100", "in-capmkt:CashFlowsFromOperating", start=FY[0], end=FY[1])
    kit.add_fact("1700", "in-capmkt:CashCfs", start=date(2025, 3, 31), end=date(2025, 3, 31))
    kit.add_fact("3655", "in-capmkt:CashCfs", start=date(2026, 3, 31), end=date(2026, 3, 31))
    run_normalize(writer, taxonomy=taxonomy)
    got = {(c.concept, c.period_end): c.value for c in writer.all}
    assert got[("opening_cash", date(2025, 3, 31))] == kit.D("1700")
    assert got[("closing_cash", date(2026, 3, 31))] == kit.D("3655")
    assert ("closing_cash", date(2025, 3, 31)) not in got


def test_pdf_cash_flow_instants_come_from_the_duration_column(kit, taxonomy, writer) -> None:
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    for label, value in (
        ("Cash at the beginning of the year", "1700.83"),
        ("Cash at the end of the year", "3655.22"),
    ):
        kit.add_fact(
            value, label=label, start=FY[0], end=FY[1], unit=None, scale=1_000_000,
            extractor="ocr", confidence=0.95, filing_id=kit.PDF_FILING,
        )  # fmt: skip
    run_normalize(writer, taxonomy=taxonomy)
    got = {(c.concept, c.period_end): c.value for c in writer.all}
    assert got[("opening_cash", date(2025, 3, 31))] == kit.D("1700830000.00")
    assert got[("closing_cash", date(2026, 3, 31))] == kit.D("3655220000.00")


def test_pdf_opening_cash_of_a_column_without_a_start_is_not_dated_at_its_end(
    kit, taxonomy, writer
) -> None:
    """Real case: HDFC's cash-flow headers gave dates only (no period kind), so the column had no
    start; the opening balance used to land on the column's end date (1,651,422.7 mn closing cash
    at 30 Sep 2025 against an "opening" of 2,395,706.8 mn dated the same day)."""
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    for label, value in (
        ("Cash at the beginning of the year", "1700.83"),
        ("Cash at the end of the year", "3655.22"),
    ):
        kit.add_fact(
            value, label=label, start=None, end=FY[1], unit=None, scale=1_000_000,
            extractor="ocr", confidence=0.95, filing_id=kit.PDF_FILING,
        )  # fmt: skip
    run_normalize(writer, taxonomy=taxonomy)
    got = {(c.concept, c.period_end): c.value for c in writer.all}
    assert not [k for k in got if k[0] == "opening_cash"]
    assert got[("closing_cash", date(2026, 3, 31))] == kit.D("3655220000.00")  # the end is certain
    assert items("normalization_issue") == []  # an unusable cell is counted, not queued


def test_equal_duplicate_is_silent_and_a_differing_one_raises_an_item(
    kit, taxonomy, writer
) -> None:
    kit.add_filing()
    kit.add_fact("910000", "in-capmkt:OtherComprehensiveIncome")
    kit.add_fact("910000", "in-capmkt:OtherComprehensiveIncomeNetOfTaxes")
    run_normalize(writer, taxonomy=taxonomy)
    assert items("normalization_issue") == []

    kit.add_filing("FIL-cccccccccccc")
    kit.add_fact("910000", "in-capmkt:OtherComprehensiveIncome", filing_id="FIL-cccccccccccc")
    kit.add_fact(
        "920000", "in-capmkt:OtherComprehensiveIncomeNetOfTaxes", filing_id="FIL-cccccccccccc"
    )
    run_normalize(writer, taxonomy=taxonomy)
    (item,) = items("normalization_issue")
    payload = json.loads(item.payload_json)
    assert payload["reason"] == "duplicate_mismatch"
    assert payload["detail"]["mapped_value"] == "910000"
    assert payload["detail"]["duplicate_value"] == "920000"
    # neither value is silently preferred: the mapped one still goes through
    assert [c.value for c in writer.all if c.filing_id == "FIL-cccccccccccc"] == [kit.D("910000")]


def test_conflicting_candidates_are_dropped_and_queued(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("100", "in-capmkt:ProfitLossForPeriod")
    kit.add_fact("200", "in-capmkt:ProfitLossForPeriod")
    kit.add_fact("50", "in-capmkt:RevenueFromOperations")
    kit.add_fact("50", "in-capmkt:RevenueFromOperations")
    run_normalize(writer, taxonomy=taxonomy)
    assert [c.concept for c in writer.all] == ["revenue"]  # equal duplicates collapse to one
    (item,) = items("normalization_issue")
    assert json.loads(item.payload_json)["reason"] == "duplicate_candidates"


def test_period_unit_and_kind_problems_are_queued(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact(
        "1", "in-capmkt:RevenueFromOperations", start=date(2026, 2, 1), end=date(2026, 3, 31)
    )
    kit.add_fact("2", "in-capmkt:BasicEps", unit="iso4217:INR")
    kit.add_fact("3", "in-capmkt:Assets", start=date(2026, 1, 1), end=date(2026, 3, 31))
    run_normalize(writer, taxonomy=taxonomy)
    reasons = sorted(json.loads(i.payload_json)["reason"] for i in items("normalization_issue"))
    assert reasons == ["period_kind_mismatch", "period_misaligned", "unit_mismatch"]
    assert writer.all == []


def test_basis_falls_back_to_the_filing_and_unknown_is_queued(kit, taxonomy, writer) -> None:
    kit.add_filing(basis="standalone")
    kit.add_fact("1", "in-capmkt:RevenueFromOperations", basis="unknown")
    run_normalize(writer, taxonomy=taxonomy)
    assert [c.basis for c in writer.all] == ["standalone"]

    kit.add_filing("FIL-dddddddddddd", basis="unknown")
    kit.add_fact(
        "1", "in-capmkt:RevenueFromOperations", basis="unknown", filing_id="FIL-dddddddddddd"
    )
    run_normalize(writer, taxonomy=taxonomy)
    assert len(writer.all) == 1
    (item,) = items("ambiguous_basis")
    assert json.loads(item.payload_json)["filing_id"] == "FIL-dddddddddddd"


def test_concept_for_another_sector_is_counted_not_mapped(kit, taxonomy, writer) -> None:
    kit.add_filing("FIL-eeeeeeeeeeee", isin=kit.BANK_ISIN, family="banking")
    kit.add_fact("1", "in-capmkt:RevenueFromOperations", filing_id="FIL-eeeeeeeeeeee")
    kit.add_fact("9", "in-capmkt:Deposits", filing_id="FIL-eeeeeeeeeeee", start=Q4[1], end=Q4[1])
    (report,) = run_normalize(writer, taxonomy=taxonomy)
    assert [c.concept for c in writer.all] == ["bank_deposits"]
    assert report.stats["unmapped"] == 1  # a bank has no indas revenue QName: family keyed lookup


def test_unknown_family_leaves_the_filing_parsed(kit, taxonomy, writer) -> None:
    kit.add_filing(family="other")
    kit.add_fact("1", "in-capmkt:RevenueFromOperations")
    (report,) = run_normalize(writer, taxonomy=taxonomy)
    assert report.status == "skipped" and "family 'other'" in (report.error or "")
    assert filing_status(kit.FILING) == "parsed" and writer.all == []


def test_writer_failure_leaves_the_filing_parsed(kit, taxonomy) -> None:
    kit.add_filing()
    kit.add_fact("1", "in-capmkt:RevenueFromOperations")

    def broken(_candidates: object) -> None:
        raise RuntimeError("disk full")

    (report,) = run_normalize(broken, taxonomy=taxonomy)
    assert report.status == "failed" and "disk full" in (report.error or "")
    assert filing_status(kit.FILING) == "parsed"
    with transaction() as conn:
        assert conn.execute(sa.select(tables.jobs.c.status)).scalar_one() == "failed"


def test_selecting_a_filing_checks_its_status(kit, taxonomy, writer) -> None:
    kit.add_filing(status="quarantined")
    with pytest.raises(NormalizeError, match="quarantined"):
        run_normalize(writer, kit.FILING, taxonomy=taxonomy)
    with pytest.raises(NormalizeError, match="unknown filing"):
        run_normalize(writer, "FIL-nope", taxonomy=taxonomy)
    assert run_normalize(writer, taxonomy=taxonomy) == []  # nothing parsed: no work, no error


def test_quarantined_and_fetched_filings_are_never_normalized(kit, taxonomy, writer) -> None:
    kit.add_filing("FIL-ffffffffffff", status="fetched")
    kit.add_filing("FIL-111111111111", status="quarantined")
    assert run_normalize(writer, taxonomy=taxonomy) == []


def test_status_moves_via_the_registry(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("1", "in-capmkt:RevenueFromOperations")
    run_normalize(writer, taxonomy=taxonomy)
    assert filing_status(kit.FILING) == "normalized"
    registry.set_status(kit.FILING, FilingStatus.PARSED)
    run_normalize(writer, taxonomy=taxonomy)
    assert len(writer.calls) == 2


def test_label_is_accepted_only_for_its_own_statement(kit, taxonomy, writer) -> None:
    """The same label on a P&L page and on a cash-flow page: only the P&L one is finance_cost."""
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    for value, statement in (("129.53", "profit_and_loss"), ("120.97", "cash_flow")):
        kit.add_fact(
            value, label="Finance costs", start=FY[0], end=FY[1], unit=None, scale=1_000_000,
            extractor="ocr", confidence=0.95, filing_id=kit.PDF_FILING, statement=statement,
        )  # fmt: skip
    (report,) = run_normalize(writer, taxonomy=taxonomy)
    (candidate,) = writer.all
    assert candidate.concept == "finance_cost" and candidate.value == kit.D("129530000.00")
    assert report.stats["issues"] == {}  # no duplicate_candidates any more
    (item,) = items("unmapped_concept")
    payload = json.loads(item.payload_json)
    assert payload["key"] == "finance costs" and payload["statement"] == "cash_flow"


def test_pdf_outflow_is_converted_to_the_xbrl_sign_and_matches(kit, taxonomy, writer) -> None:
    kit.add_filing()
    kit.add_fact("141630000", "in-capmkt:DividendsPaid", start=FY[0], end=FY[1])
    kit.add_filing(kit.PDF_FILING, fmt="pdf", family=None)
    kit.add_fact(
        "-141.63", label="Dividends paid", start=FY[0], end=FY[1], unit=None, scale=1_000_000,
        extractor="ocr", confidence=0.95, filing_id=kit.PDF_FILING, statement="cash_flow",
    )  # fmt: skip
    run_normalize(writer, taxonomy=taxonomy)
    values = {c.extractor: c.value for c in writer.all}
    assert values["xbrl"] == kit.D("141630000")
    assert values["ocr"] == kit.D("141630000.00") and values["ocr"] == values["xbrl"]


def test_a_placeholder_zero_is_counted_not_stored(kit, taxonomy, writer) -> None:
    kit.add_filing("FIL-eeeeeeeeeeee", isin=kit.BANK_ISIN, family="banking")
    kit.add_fact("0", "in-capmkt:GrossNonPerformingAssets", filing_id="FIL-eeeeeeeeeeee")
    kit.add_filing("FIL-999999999999", isin=kit.BANK_ISIN, family="banking")
    kit.add_fact("358463500000", "in-capmkt:GrossNonPerformingAssets", filing_id="FIL-999999999999")
    reports = {r.filing_id: r for r in run_normalize(writer, taxonomy=taxonomy)}
    assert reports["FIL-eeeeeeeeeeee"].stats["placeholder_zero"] == 1
    assert [(c.filing_id, c.concept, c.value) for c in writer.all] == [
        ("FIL-999999999999", "bank_gross_npa", kit.D("358463500000"))
    ]
    assert items("normalization_issue") == [] and items("unmapped_concept") == []
