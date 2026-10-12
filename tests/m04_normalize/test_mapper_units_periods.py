"""Mapper, units, periods and basis."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finrag.core.types import Basis
from finrag.normalize.basis import resolve_basis
from finrag.normalize.mapper import (
    KEY_LABEL,
    KEY_QNAME,
    Known,
    Mapped,
    Mapper,
    NotApplicable,
    Resolutions,
    Unmapped,
)
from finrag.normalize.periods import ANCHOR_DAY_BEFORE_START, ANCHOR_END, classify
from finrag.normalize.units import to_canonical
from finrag.parsing.models import ParsedFact

# --- mapper ----------------------------------------------------------------------------------


def xbrl(qname: str, value: str = "1") -> ParsedFact:
    return ParsedFact(Decimal(value), concept_raw=qname, unit_raw="iso4217:INR")


def pdf(label: str, statement: str | None = None, value: str = "1") -> ParsedFact:
    return ParsedFact(
        Decimal(value),
        label_raw=label,
        extractor="pdf_table",
        scale=1_000_000,
        statement_type=statement,
    )


def test_xbrl_standard_and_family(mapper) -> None:
    assert mapper.map_fact(xbrl("in-capmkt:RevenueFromOperations"), "indas", "general") == Mapped(
        "revenue", "standard"
    )
    # the same QName under the other family is not a mapping
    result = mapper.map_fact(xbrl("in-capmkt:RevenueFromOperations"), "banking", "bank")
    assert isinstance(result, Unmapped) and result.key_type == KEY_QNAME


def test_bank_family_maps_bank_qnames(mapper) -> None:
    assert mapper.map_fact(xbrl("in-capmkt:ProfitLossForThePeriod"), "banking", "bank") == Mapped(
        "profit_after_tax", "standard"
    )


def test_label_exact_after_normalisation(mapper) -> None:
    for label in (
        "Revenue from Operations",
        "  revenue   FROM operations",
        "(a) Revenue from operations",
    ):
        assert mapper.map_fact(pdf(label), "", "general") == Mapped("revenue", "label")


def test_unknown_label_is_unmapped_without_suggestion(mapper) -> None:
    result = mapper.map_fact(pdf("Something Entirely Different"), "", "general")
    assert result == Unmapped("something entirely different", KEY_LABEL, None, None)


def test_fuzzy_label_is_only_a_suggestion(mapper) -> None:
    result = mapper.map_fact(pdf("Revenue from operation"), "", "general")
    assert isinstance(result, Unmapped)
    assert result.suggestion is not None
    assert result.suggestion["concept"] == "revenue"
    assert float(result.suggestion["score"]) >= 85  # type: ignore[arg-type]


def test_known_unmapped_is_not_queued(mapper) -> None:
    result = mapper.map_fact(xbrl("in-capmkt:AdjustmentsForWorkingCapital"), "indas", "general")
    assert isinstance(result, Known) and result.reason == "cash_flow_adjustment"
    duplicate = mapper.map_fact(
        xbrl("in-capmkt:OtherComprehensiveIncomeNetOfTaxes"), "indas", "general"
    )
    assert (
        isinstance(duplicate, Known)
        and duplicate.entry.duplicate_of == "other_comprehensive_income"
    )


def test_concept_outside_the_sector_is_not_applicable(mapper) -> None:
    assert mapper.map_fact(xbrl("in-capmkt:RevenueFromOperations"), "indas", "bank") == (
        NotApplicable("revenue", "standard")
    )
    assert mapper.map_fact(pdf("Net sales"), "", "bank") == NotApplicable("revenue", "label")


def test_manual_resolutions_apply(taxonomy) -> None:
    resolutions = Resolutions(
        qnames={("indas", "in-capmkt:Odd"): "cfo"},
        labels={("", "odd line"): "revenue", ("cash_flow", "odd flow"): "cfo"},
        ignored_qnames={("indas", "in-capmkt:Skip")},
        ignored_labels={("", "skip me")},
    )
    mapper = Mapper(taxonomy, resolutions, fuzzy_min_score=85)
    assert mapper.map_fact(xbrl("in-capmkt:Odd"), "indas", "general") == Mapped("cfo", "manual")
    assert mapper.map_fact(pdf("Odd line"), "", "general") == Mapped("revenue", "manual")
    assert mapper.map_fact(pdf("Odd flow", "cash_flow"), "", "general") == Mapped("cfo", "manual")
    # a resolution made on one statement does not apply on another
    assert isinstance(mapper.map_fact(pdf("Odd flow", "balance_sheet"), "", "general"), Unmapped)
    assert mapper.map_fact(xbrl("in-capmkt:Skip"), "indas", "general") == Known("user_ignored")
    assert mapper.map_fact(pdf("Skip me"), "", "general") == Known("user_ignored")


def test_label_maps_only_within_its_own_statement(mapper) -> None:
    ok = mapper.map_fact(pdf("Revenue from operations", "profit_and_loss"), "", "general")
    assert ok == Mapped("revenue", "label")
    wrong = mapper.map_fact(pdf("Revenue from operations", "cash_flow"), "", "general")
    assert wrong == Unmapped("revenue from operations", KEY_LABEL, None, "cash_flow")


def test_segment_pages_are_known_unmapped(mapper) -> None:
    result = mapper.map_fact(pdf("Revenue from operations", "segment"), "", "general")
    assert result == Known("segment")


def test_fact_without_a_name_is_unmapped(mapper) -> None:
    assert isinstance(mapper.map_fact(ParsedFact(Decimal(1)), "indas", "general"), Unmapped)


# --- units -----------------------------------------------------------------------------------


def test_crore_scale_is_exact_decimal(taxonomy) -> None:
    fact = ParsedFact(Decimal("1234.56"), label_raw="x", extractor="pdf_table", scale=10_000_000)
    result = to_canonical(fact, taxonomy.concepts["revenue"], taxonomy)
    assert result.value == Decimal("12345600000")
    assert isinstance(result.value, Decimal)


def test_million_scale(taxonomy) -> None:
    fact = ParsedFact(Decimal("7737.02"), label_raw="x", extractor="ocr", scale=1_000_000)
    assert to_canonical(fact, taxonomy.concepts["revenue"], taxonomy).value == Decimal("7737020000")


def test_pdf_outflows_are_converted_to_the_xbrl_sign(taxonomy) -> None:
    printed = ParsedFact(Decimal("-141.63"), label_raw="x", extractor="ocr", scale=1_000_000)
    got = to_canonical(printed, taxonomy.concepts["dividends_paid"], taxonomy)
    assert got.value == Decimal("141630000.00")  # same sign as the XBRL value
    positive = ParsedFact(Decimal("141.63"), label_raw="x", extractor="ocr", scale=1_000_000)
    # a PDF that prints it positive is genuinely different: the sign error is not hidden
    assert to_canonical(positive, taxonomy.concepts["dividends_paid"], taxonomy).value < 0
    xbrl_fact = xbrl("in-capmkt:DividendsPaid", "141630000")
    assert to_canonical(xbrl_fact, taxonomy.concepts["dividends_paid"], taxonomy).value == Decimal(
        "141630000"
    )
    # concepts without pdf_sign keep what is printed
    cfo = ParsedFact(Decimal("-132.11"), label_raw="x", extractor="ocr", scale=1_000_000)
    assert to_canonical(cfo, taxonomy.concepts["cfo"], taxonomy).value == Decimal("-132110000.00")


def test_pdf_without_unit_line_is_rejected(taxonomy) -> None:
    fact = ParsedFact(Decimal("5"), label_raw="x", extractor="pdf_table", scale=None)
    result = to_canonical(fact, taxonomy.concepts["revenue"], taxonomy)
    assert result.value is None and result.reason == "unit_missing"


def test_per_share_values_are_never_scaled(taxonomy) -> None:
    fact = ParsedFact(Decimal("4.63"), label_raw="x", extractor="pdf_table", scale=1_000_000)
    assert to_canonical(fact, taxonomy.concepts["eps_basic"], taxonomy).value == Decimal("4.63")
    unitless = ParsedFact(Decimal("4.63"), label_raw="x", extractor="pdf_table", scale=None)
    assert to_canonical(unitless, taxonomy.concepts["eps_basic"], taxonomy).value == Decimal("4.63")


def test_xbrl_values_are_already_rupees(taxonomy) -> None:
    fact = xbrl("in-capmkt:RevenueFromOperations", "12095930000")
    assert to_canonical(fact, taxonomy.concepts["revenue"], taxonomy).value == Decimal(
        "12095930000"
    )


def test_xbrl_unit_must_match_the_concept(taxonomy) -> None:
    fact = ParsedFact(Decimal("4.63"), concept_raw="x", unit_raw="xbrli:pure")
    result = to_canonical(fact, taxonomy.concepts["eps_basic"], taxonomy)
    assert result.value is None and result.reason == "unit_mismatch"


def test_nil_value_is_reported(taxonomy) -> None:
    result = to_canonical(ParsedFact(None, concept_raw="x"), taxonomy.concepts["revenue"], taxonomy)
    assert result.value is None and result.reason == "nil"


# --- periods ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 1, 1), date(2026, 3, 31), "Q"),
        (date(2025, 4, 1), date(2025, 9, 30), "H"),
        (date(2025, 4, 1), date(2025, 12, 31), "9M"),
        (date(2025, 4, 1), date(2026, 3, 31), "FY"),
        (date(2025, 10, 1), date(2026, 3, 31), "H"),
    ],
)
def test_duration_classes(start: date, end: date, expected: str) -> None:
    result = classify(start, end, "duration")
    assert result.period is not None and result.period.type.value == expected


def test_misaligned_duration_is_rejected() -> None:
    result = classify(date(2025, 5, 1), date(2025, 7, 31), "duration")
    assert result.period is None and result.reason == "period_misaligned"
    assert classify(date(2025, 4, 2), date(2026, 3, 31), "duration").reason == "period_misaligned"


def test_instants() -> None:
    result = classify(date(2026, 3, 31), date(2026, 3, 31), "instant")
    assert result.period is not None and result.period.type.value == "INSTANT"
    assert classify(None, date(2026, 3, 31), "instant").period is not None


def test_kind_mismatch_and_missing_end() -> None:
    assert classify(date(2026, 1, 1), date(2026, 3, 31), "instant").reason == "period_kind_mismatch"
    assert (
        classify(date(2026, 3, 31), date(2026, 3, 31), "duration").reason == "period_kind_mismatch"
    )
    assert classify(None, None, "duration").reason == "period_missing"


def test_cash_flow_instants_from_a_duration_column() -> None:
    start, end = date(2025, 4, 1), date(2026, 3, 31)
    closing = classify(start, end, "instant", ANCHOR_END)
    opening = classify(start, end, "instant", ANCHOR_DAY_BEFORE_START)
    assert closing.period is not None and closing.period.end == date(2026, 3, 31)
    assert opening.period is not None and opening.period.end == date(2025, 3, 31)


# --- basis -----------------------------------------------------------------------------------


def test_basis_order() -> None:
    assert resolve_basis("standalone", "consolidated") is Basis.STANDALONE
    assert resolve_basis("unknown", "consolidated") is Basis.CONSOLIDATED
    assert resolve_basis(None, "standalone") is Basis.STANDALONE
    assert resolve_basis("unknown", None) is Basis.UNKNOWN
    assert resolve_basis(None, "unknown") is Basis.UNKNOWN
