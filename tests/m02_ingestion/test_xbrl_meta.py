from datetime import date

import pytest

from finrag.core.types import Basis, PeriodType, is_valid_isin
from finrag.ingestion.xbrl_meta import XbrlParseError, parse_xbrl_meta


def test_reads_all_filing_level_fields(kit) -> None:
    meta = parse_xbrl_meta(kit.make_xbrl())
    assert meta.isin == kit.ALPHA and meta.isin_valid is True
    assert (meta.symbol, meta.scrip_code) == ("ALPHAX", "500001")
    assert meta.name == "ALPHA INDUSTRIES LIMITED"
    assert meta.basis is Basis.CONSOLIDATED
    assert (meta.period_start, meta.period_end) == (date(2026, 1, 1), date(2026, 3, 31))
    assert meta.period_type_raw == "Quarterly" and meta.declared_period_type is PeriodType.Q
    assert meta.reporting_quarter == "Fourth quarter" and meta.audited == "Audited"
    assert meta.level_of_rounding == "Millions"
    assert meta.board_meeting_date == date(2026, 5, 2)
    assert meta.taxonomy_family == "indas"


def test_first_occurrence_wins_over_the_cumulative_context(kit) -> None:
    meta = parse_xbrl_meta(kit.make_xbrl(start="2026-01-01"))  # a FourD start of 2025-04-01 follows
    assert meta.period_start == date(2026, 1, 1)


def test_banking_family_comes_from_the_entity_namespace(kit) -> None:
    assert parse_xbrl_meta(kit.make_xbrl(family="Banking")).taxonomy_family == "banking"


def test_typo_isin_fails_the_check_digit(kit) -> None:
    assert not is_valid_isin(kit.ALPHA_TYPO)
    meta = parse_xbrl_meta(kit.make_xbrl(isin=kit.ALPHA_TYPO))
    assert meta.isin == kit.ALPHA_TYPO and meta.isin_valid is False


def test_half_yearly_is_recognised_and_span_agrees(kit) -> None:
    meta = parse_xbrl_meta(
        kit.make_xbrl(start="2025-10-01", end="2026-03-31", period_type="Half Yearly")
    )
    assert meta.declared_period_type is PeriodType.H and meta.span_period_type is PeriodType.H


def test_unknown_period_type_value_is_kept_raw_not_interpreted(kit) -> None:
    meta = parse_xbrl_meta(kit.make_xbrl(period_type="Something New"))
    assert meta.period_type_raw == "Something New" and meta.declared_period_type is None


def test_missing_elements_are_none() -> None:
    meta = parse_xbrl_meta(b"<root xmlns:a='http://example.com/x'><a:Other>1</a:Other></root>")
    assert meta.isin is None and meta.isin_valid is None and meta.period_end is None
    assert meta.basis is None and meta.taxonomy_family is None and meta.board_meeting_date is None


def test_missing_board_date_is_none(kit) -> None:
    assert parse_xbrl_meta(kit.make_xbrl(board_date=None)).board_meeting_date is None


def test_not_xml_raises() -> None:
    with pytest.raises(XbrlParseError):
        parse_xbrl_meta(b"this is not xml")
