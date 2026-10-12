"""XBRL parsing against the synthetic instance."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from finrag.ingestion.xbrl_meta import XbrlParseError
from finrag.parsing.models import Dimension, dimensions_json
from finrag.parsing.xbrl_parser import parse_xbrl


@pytest.fixture
def parsed(kit):
    return parse_xbrl(kit.xbrl.build_instance(), "standalone")


def _find(parsed, concept: str, context: str):
    return next(
        f
        for f in parsed.facts
        if f.concept_raw == f"in-capmkt:{concept}" and f.source_loc.startswith(context + " ")
    )


def test_every_numeric_fact_is_kept(kit, parsed) -> None:
    expected = kit.xbrl.Expected()
    # the nil fact is a numeric fact too: never dropped
    assert len(parsed.facts) == expected.numeric + expected.nil
    assert len(parsed.text_facts) == expected.text


def test_period_unit_decimals_and_value(parsed) -> None:
    revenue = _find(parsed, "RevenueFromOperations", "OneD")
    assert (revenue.period_start, revenue.period_end) == (date(2026, 1, 1), date(2026, 3, 31))
    assert revenue.value == Decimal("7737020000")
    assert revenue.unit_raw == "iso4217:INR"
    assert revenue.decimals == -4
    assert (revenue.scale, revenue.confidence, revenue.extractor) == (0, 1.0, "xbrl")
    assert (revenue.basis, revenue.concept_raw) == ("standalone", "in-capmkt:RevenueFromOperations")
    year = _find(parsed, "RevenueFromOperations", "FourD")
    assert (year.period_start, year.period_end) == (date(2025, 4, 1), date(2026, 3, 31))
    assert year.value == Decimal("21835630000")


def test_instant_has_start_equal_end(parsed) -> None:
    assets = _find(parsed, "Assets", "OneI")
    assert assets.period_start == assets.period_end == date(2026, 3, 31)


def test_negative_and_exact_decimal_values(parsed) -> None:
    assert _find(parsed, "ProfitLossForPeriod", "OneD").value == Decimal("-705930000")
    eps = _find(parsed, "BasicEarningsPerShare", "OneD")
    assert eps.value == Decimal("12.43")
    assert isinstance(eps.value, Decimal)


def test_divide_unit_and_pure_unit(parsed) -> None:
    assert _find(parsed, "BasicEarningsPerShare", "OneD").unit_raw == "iso4217:INR/xbrli:shares"
    assert _find(parsed, "ShareHoldingPercentage", "OneI").unit_raw == "xbrli:pure"


def test_decimals_inf_is_stored_as_none(parsed) -> None:
    assert _find(parsed, "BasicEarningsPerShare", "OneD").decimals is None
    assert _find(parsed, "PaidUpValueOfEquityShareCapital", "OneI").decimals is None
    assert _find(parsed, "Remuneration", "AuditorD").decimals == 0


def test_nil_numeric_fact_has_no_value(parsed) -> None:
    nil = _find(parsed, "ExceptionalItems", "OneD")
    assert nil.value is None
    assert nil.unit_raw == "iso4217:INR"


def test_dimensions_explicit_and_typed(kit, parsed) -> None:
    other = _find(parsed, "OtherExpenses", "OneExpenses1D")
    assert other.dimensions == (
        Dimension("in-capmkt:DetailsOfOtherExpensesAxis", "in-capmkt:OtherExpenses1Member"),
    )
    assert "in-capmkt:DetailsOfOtherExpensesAxis=in-capmkt:OtherExpenses1Member" in other.source_loc
    segment = _find(parsed, "RevenueFromOperations", "OneSegD")
    assert segment.dimensions == (Dimension("in-capmkt:ReportableSegmentsAxis", "Segment1", True),)
    assert json.loads(dimensions_json(segment.dimensions)) == [
        {"axis": "in-capmkt:ReportableSegmentsAxis", "member": "Segment1", "typed": True}
    ]
    assert sum(1 for f in parsed.facts if f.dimensions) == kit.xbrl.Expected().dimensioned


def test_entity_totals_have_no_dimensions(parsed) -> None:
    assert _find(parsed, "RevenueFromOperations", "OneD").dimensions == ()
    assert dimensions_json(()) is None


def test_source_loc_is_context_and_concept(parsed) -> None:
    assert _find(parsed, "OtherIncome", "OneD").source_loc == "OneD in-capmkt:OtherIncome"


def test_text_facts_are_kept_separately(parsed) -> None:
    by_concept = {t.concept_raw: t for t in parsed.text_facts}
    assert by_concept["in-capmkt:ISIN"].value == "INE111A01010"
    assert by_concept["in-capmkt:DescriptionOfOtherExpenses"].value == "Freight & packing"
    assert by_concept["in-capmkt:ISIN"].context_ref == "OneD"
    assert by_concept["in-capmkt:ISIN"].period_end == date(2026, 3, 31)
    assert all(f.concept_raw not in by_concept for f in parsed.facts)


def test_not_xml_raises() -> None:
    with pytest.raises(XbrlParseError, match="not valid XML"):
        parse_xbrl(b"%PDF-1.4 not xml")


def test_unknown_context_raises() -> None:
    data = (
        b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:a="urn:a">'
        b'<a:Thing contextRef="Nope" unitRef="U" decimals="0">1</a:Thing></xbrli:xbrl>'
    )
    with pytest.raises(XbrlParseError, match="unknown contextRef"):
        parse_xbrl(data)


def test_unparseable_number_raises_not_skipped(kit) -> None:
    data = kit.xbrl.build_instance().replace(b">7737020000<", b">seven<")
    with pytest.raises(XbrlParseError, match="not a number"):
        parse_xbrl(data)


def test_facts_are_not_scaled_by_level_of_rounding(parsed) -> None:
    # XBRL values are full rupees whatever LevelOfRounding says (PROGRESS finding 6)
    assert _find(parsed, "OtherIncome", "OneD").value == Decimal("102350000")
