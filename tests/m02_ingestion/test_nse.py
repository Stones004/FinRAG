"""NSE parsing against the real responses recorded in tests/fixtures/nse/ (no network)."""

import json
from datetime import date
from pathlib import Path

import pytest

from finrag.core.config import get_settings
from finrag.core.types import Basis, FilingFormat, PeriodType
from finrag.ingestion.adapters.base import AdapterError
from finrag.ingestion.adapters.nse import (
    NseAdapter,
    last_completed_quarter,
    parse_annual_reports,
    parse_integrated,
    parse_legacy,
    parse_transcripts,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nse"
ISIN, SYMBOL = "INE0NT901020", "NETWEB"


def load(name: str):
    """The recorded response body (the `response` key of the fixture file)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["response"]


def by_url(candidates):
    return {c.source_url.rsplit("/", 1)[-1]: c for c in candidates}


def test_integrated_original_uses_broadcast_date() -> None:
    cands = by_url(parse_integrated(load("integrated_filing_results.json")["data"], ISIN, SYMBOL))
    original = cands["INTEGRATED_FILING_INDAS_1702930_28072026033426_WEB.xml"]
    assert (original.filing_type, original.format, original.submission) == (
        "financial_results", FilingFormat.XBRL, "original",
    )  # fmt: skip
    assert original.filed_on == "2026-07-28T15:32:17"
    assert (original.basis, original.period_type) == (Basis.STANDALONE, PeriodType.Q)
    assert (original.period_start, original.period_end) == (date(2026, 4, 1), date(2026, 6, 30))
    assert (original.isin, original.identifier) == (ISIN, SYMBOL)


def test_integrated_revision_uses_revised_date_because_broadcast_date_is_null() -> None:
    rows = load("integrated_filing_results.json")["data"]
    assert rows[1]["broadcast_Date"] is None and rows[1]["revised_Date"]
    revision = by_url(parse_integrated(rows, ISIN, SYMBOL))[
        "INTEGRATED_FILING_INDAS_1668217_16052026103806_WEB.xml"
    ]
    assert revision.submission == "revision" and revision.revision_no is None
    assert revision.filed_on == "2026-05-16T10:38:05"
    assert revision.period_end == date(2026, 3, 31)


def test_integrated_pdf_attachment_becomes_a_pdf_candidate_and_null_does_not() -> None:
    cands = parse_integrated(load("integrated_filing_results.json")["data"], ISIN, SYMBOL)
    pdfs = [c for c in cands if c.format is FilingFormat.PDF]
    assert [c.source_url.rsplit("/", 1)[-1] for c in pdfs] == [
        "NETWEB1234_02052026192032_Netweb_Q4_Results.pdf"
    ]
    assert pdfs[0].filed_on == "2026-05-02T19:20:54" and len(cands) == 4  # 3 xbrl + 1 pdf


def test_legacy_rows_become_quarterly_xbrl_and_ignore_the_row_isin() -> None:
    rows = load("corporates_financial_results.json")
    cands = parse_legacy(rows, "INE000000000", SYMBOL)
    assert len(cands) == 2 and {c.isin for c in cands} == {"INE000000000"}
    first = cands[0]
    assert first.basis is Basis.STANDALONE and first.period_type is PeriodType.Q
    assert (first.period_start, first.period_end) == (date(2024, 10, 1), date(2024, 12, 31))
    assert first.filed_on == "2025-03-10T11:05:22"
    assert first.source_url.endswith("INDAS_121025_1394141_10032025110522.xml")


def test_legacy_rows_covered_by_integrated_filings_are_skipped() -> None:
    rows = load("corporates_financial_results.json")
    assert parse_legacy(rows, ISIN, SYMBOL, before=date(2024, 12, 31)) == []
    assert len(parse_legacy(rows, ISIN, SYMBOL, before=date(2025, 3, 31))) == 2


def test_annual_reports_take_the_fiscal_year_from_to_yr() -> None:
    cands = parse_annual_reports(load("annual_reports.json")["data"], ISIN, SYMBOL)
    assert [c.period_end for c in cands] == [date(2026, 3, 31), date(2025, 3, 31)]
    first = cands[0]
    assert (first.filing_type, first.format, first.period_type) == (
        "annual_report", FilingFormat.PDF, PeriodType.FY,
    )  # fmt: skip
    assert first.period_start == date(2025, 4, 1) and first.submission == "original"
    assert first.filed_on == "2026-08-28T13:00:44" and first.basis is Basis.UNKNOWN


def test_only_transcript_announcements_are_kept() -> None:
    rows = load("corporate_announcements.json")
    texts = [r["attchmntText"] for r in rows]
    assert "Transcript" in texts[0] and "Schedule of meet" in texts[1]  # same desc, only one is
    (cand,) = parse_transcripts(rows, ISIN, SYMBOL)
    assert cand.filing_type == "earnings_call_transcript" and cand.format is FilingFormat.PDF
    assert cand.source_url.endswith("NETWEB1234_01082026124230_Transcript_Q1_2027.pdf")
    assert cand.filed_on == "2026-08-01T12:42:46"


def test_transcript_period_is_a_flagged_guess_of_the_last_completed_quarter() -> None:
    (cand,) = parse_transcripts(load("corporate_announcements.json"), ISIN, SYMBOL)
    assert (cand.period_start, cand.period_end) == (date(2026, 4, 1), date(2026, 6, 30))
    assert cand.period_unverified is True


def test_transcript_matching_is_case_insensitive_and_from_settings() -> None:
    row = dict(
        load("corporate_announcements.json")[0], attchmntText="has informed about TRANSCRIPT"
    )
    assert len(parse_transcripts([row], ISIN, SYMBOL)) == 1
    other_desc = dict(row, desc="Something Else")
    assert parse_transcripts([other_desc], ISIN, SYMBOL) == []


@pytest.mark.parametrize(
    ("on", "end"),
    [
        (date(2026, 8, 1), date(2026, 6, 30)),
        (date(2026, 5, 2), date(2026, 3, 31)),
        (date(2026, 1, 20), date(2025, 12, 31)),
        (date(2026, 7, 1), date(2026, 6, 30)),
        (date(2026, 6, 30), date(2026, 3, 31)),
    ],
)
def test_last_completed_quarter(on: date, end: date) -> None:
    assert last_completed_quarter(on)[1] == end


def test_rows_with_missing_fields_are_skipped_not_guessed() -> None:
    assert (
        parse_integrated([{"type_Sub": "Original", "xbrl": "https://x/y.xml"}], ISIN, SYMBOL) == []
    )
    assert parse_annual_reports([{"fileName": "https://x/y.pdf"}], ISIN, SYMBOL) == []
    assert parse_legacy([{"fromDate": "01-Oct-2024"}], ISIN, SYMBOL) == []


class FakeHttp:
    """Serves the recorded responses by endpoint path and records every call."""

    def __init__(self) -> None:
        endpoints = get_settings().sources.nse.endpoints
        self.routes = {
            endpoints.integrated_filing_results: load("integrated_filing_results.json"),
            endpoints.legacy_financial_results: load("corporates_financial_results.json"),
            endpoints.annual_reports: load("annual_reports.json"),
            endpoints.corporate_announcements: load("corporate_announcements.json"),
        }
        self.calls: list[tuple[str, dict]] = []
        self.downloads: list[str] = []

    def get_json(self, path, params):
        self.calls.append((path, params))
        return self.routes[path]

    def get_bytes(self, url):
        self.downloads.append(url)
        return b"bytes"


class Lookup:
    def symbol_for(self, isin):
        return SYMBOL if isin == ISIN else None


def adapter(http=None):
    return NseAdapter(get_settings(), http or FakeHttp(), Lookup(), today=date(2026, 10, 12))


def test_adapter_queries_all_four_listings_with_config_driven_parameters() -> None:
    http = FakeHttp()
    cands = adapter(http).list_filings(ISIN, date(2025, 1, 1))
    endpoints = get_settings().sources.nse.endpoints
    paths = [p for p, _ in http.calls]
    assert paths[0] == endpoints.integrated_filing_results
    assert endpoints.legacy_financial_results in paths and endpoints.annual_reports in paths
    assert paths.count(endpoints.corporate_announcements) == 8  # 650 days in 90-day windows
    first = http.calls[0][1]
    assert first["symbol"] == SYMBOL and first["type"] == "Integrated Filing- Financials"
    assert (first["from_date"], first["to_date"]) == ("01-01-2025", "12-10-2026")
    kinds = {c.filing_type for c in cands}
    assert kinds == {"financial_results", "annual_report", "earnings_call_transcript"}
    assert len(cands) == len({c.source_url for c in cands})  # repeated windows are de-duplicated


def test_adapter_since_drops_older_filings() -> None:
    cands = adapter().list_filings(ISIN, date(2026, 7, 1))
    assert cands and all(c.filed_on[:10] >= "2026-07-01" for c in cands)


def test_adapter_fetch_downloads_the_candidate_url() -> None:
    http = FakeHttp()
    nse = adapter(http)
    cand = nse.list_filings(ISIN, date(2026, 7, 1))[0]
    assert nse.fetch(cand) == b"bytes" and http.downloads == [cand.download_url]


def test_adapter_needs_a_known_symbol() -> None:
    with pytest.raises(AdapterError, match="symbol"):
        adapter().list_filings("INE000000000", None)
