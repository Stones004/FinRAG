from datetime import date
from pathlib import Path

import pytest

from finrag.core.config import get_settings
from finrag.core.types import Basis, FilingFormat, PeriodType
from finrag.ingestion.adapters.local_inbox import BadName, LocalInboxAdapter, parse_name
from finrag.ingestion.registry import DbEntityLookup


def parse(name: str):
    return parse_name(Path(name), get_settings())


def test_full_name_gives_every_field() -> None:
    c = parse("INE524A01029__financial_results__Q4FY26__consolidated__rev1.xml")
    assert (c.identifier, c.isin) == ("INE524A01029", "INE524A01029")
    assert (c.filing_type, c.format) == ("financial_results", FilingFormat.XBRL)
    assert (c.submission, c.revision_no) == ("revision", 1)
    assert (c.basis, c.period_type) == (Basis.CONSOLIDATED, PeriodType.Q)
    assert (c.period_start, c.period_end) == (date(2026, 1, 1), date(2026, 3, 31))
    assert c.filed_on is None


def test_symbol_identifier_leaves_isin_for_the_lookup() -> None:
    c = parse("NETWEB__financial_results__Q2FY26__standalone.xml")
    assert c.identifier == "NETWEB" and c.isin is None and c.submission == "original"
    assert c.revision_no is None


def test_annual_report_is_a_fiscal_year_pdf() -> None:
    c = parse("INE524A01029__annual_report__FY26__consolidated.pdf")
    assert (c.period_type, c.period_end, c.format) == (PeriodType.FY, date(2026, 3, 31), "pdf")


def test_transcript_period_is_the_quarter_discussed() -> None:
    c = parse("INE040A01034__earnings_call_transcript__Q1FY27__consolidated.pdf")
    assert (c.filing_type, c.period_type, c.period_start) == (
        "earnings_call_transcript", PeriodType.Q, date(2026, 4, 1),
    )  # fmt: skip
    assert c.filed_on is None


@pytest.mark.parametrize(
    "name",
    [
        "report.pdf",
        "A__financial_results__Q4FY26.xml",
        "A__mystery__Q4FY26__consolidated.xml",
        "A__financial_results__Q9FY26__consolidated.xml",
        "A__financial_results__Q4FY26__both.xml",
        "A__annual_report__Q4FY26__consolidated.pdf",
        "A__annual_report__FY26__consolidated.xml",
        "A__earnings_call_transcript__FY26__consolidated.pdf",
        "A__earnings_call_transcript__Q1FY27__consolidated.xml",
        "A__financial_results__Q4FY26__consolidated__rev.xml",
        "A__financial_results__Q4FY26__consolidated.docx",
    ],
)
def test_bad_names_raise(name: str) -> None:
    with pytest.raises(BadName):
        parse(name)


def adapter() -> LocalInboxAdapter:
    return LocalInboxAdapter(DbEntityLookup())


def test_scan_resolves_symbols_and_filters_by_isin(db, kit, inbox) -> None:
    (inbox / "ALPHAX__financial_results__Q4FY26__consolidated.xml").write_bytes(kit.make_xbrl())
    (inbox / f"{kit.BETA}__annual_report__FY26__standalone.pdf").write_bytes(b"%PDF")
    a = adapter()
    a.begin_run()
    assert [c.isin for c in a.list_filings(kit.ALPHA, None)] == [kit.ALPHA]
    assert [c.isin for c in a.list_filings(kit.BETA, None)] == [kit.BETA]
    assert a.list_filings("INE000000000", None) == [] and a.rejected() == []


def test_free_form_xbrl_name_is_read_from_content(db, kit, inbox) -> None:
    (inbox / "results (1).xml").write_bytes(kit.make_xbrl())
    (c,) = adapter().list_filings(kit.ALPHA, None)
    assert (c.isin, c.basis, c.period_type, c.period_end) == (
        kit.ALPHA, Basis.CONSOLIDATED, PeriodType.Q, date(2026, 3, 31),
    )  # fmt: skip
    assert c.filing_type == "financial_results" and c.local_path.name == "results (1).xml"


def test_free_form_name_with_a_typo_isin_falls_back_to_the_symbol(db, kit, inbox) -> None:
    (inbox / "download.xml").write_bytes(kit.make_xbrl(isin=kit.ALPHA_TYPO, symbol="ALPHAX"))
    (c,) = adapter().list_filings(kit.ALPHA, None)
    assert c.identifier == "ALPHAX" and c.isin == kit.ALPHA


def test_unreadable_or_unnamed_files_are_rejected_with_a_reason(db, kit, inbox) -> None:
    (inbox / "notes.pdf").write_bytes(b"%PDF")
    (inbox / "broken.xml").write_bytes(b"not xml")
    (inbox / "GHOST__annual_report__FY26__consolidated.pdf").write_bytes(b"%PDF")
    a = adapter()
    a.begin_run()
    rejected = {r.file_name: r.reason for r in a.rejected()}
    assert set(rejected) == {
        "notes.pdf",
        "broken.xml",
        "GHOST__annual_report__FY26__consolidated.pdf",
    }
    assert "tracked company" in rejected["GHOST__annual_report__FY26__consolidated.pdf"]
    assert all(r for r in rejected.values())


def test_processed_folder_is_not_scanned(db, kit, inbox) -> None:
    done = inbox / "_processed"
    done.mkdir()
    (done / "ALPHAX__financial_results__Q4FY26__consolidated.xml").write_bytes(kit.make_xbrl())
    a = adapter()
    a.begin_run()
    assert a.all_candidates() == [] and a.rejected() == []


def test_missing_inbox_folder_is_just_empty(db) -> None:
    a = adapter()
    a.begin_run()
    assert a.all_candidates() == []
