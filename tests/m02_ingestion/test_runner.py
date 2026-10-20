import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from finrag.cli import app
from finrag.core.config import ConfigError, get_settings
from finrag.core.db import tables, transaction
from finrag.core.types import Basis, FilingFormat, PeriodType
from finrag.ingestion import registry
from finrag.ingestion.adapters.base import SourceAdapter
from finrag.ingestion.models import FilingCandidate
from finrag.ingestion.runner import build_adapters, run_inbox, run_ingest

STEM = "ALPHAX__financial_results__Q4FY26__consolidated"


def all_rows(table):
    with transaction() as conn:
        return list(conn.execute(sa.select(table)))


def only_filing():
    (row,) = all_rows(tables.filings)
    return row


def conflict_fields() -> list[str]:
    (item,) = [r for r in all_rows(tables.review_queue) if r.item_type == "filing_conflict"]
    return [c["field"] for c in json.loads(item.payload_json)["conflicts"]]


def test_xbrl_filed_on_is_the_board_meeting_date_and_family_is_stored(db, kit, inbox) -> None:
    (inbox / f"{STEM}.xml").write_bytes(kit.make_xbrl(board_date="2026-05-02", family="Banking"))
    run_inbox()
    row = only_filing()
    assert (row.filed_on, row.taxonomy_family, row.status) == ("2026-05-02", "banking", "fetched")


def test_missing_board_date_leaves_filed_on_empty(db, kit, inbox) -> None:
    (inbox / f"{STEM}.xml").write_bytes(kit.make_xbrl(board_date=None))
    run_inbox()
    assert only_filing().filed_on is None


def test_pdf_from_the_inbox_has_no_filed_on(db, kit, inbox) -> None:
    (inbox / f"{kit.ALPHA}__annual_report__FY26__consolidated.pdf").write_bytes(
        kit.make_pdf(["ar"])
    )
    run_inbox()
    assert only_filing().filed_on is None


def test_original_and_revision_share_filed_on_and_revision_no_orders_them(db, kit, inbox) -> None:
    (inbox / f"{STEM}.xml").write_bytes(kit.make_xbrl())
    (inbox / f"{STEM}__rev1.xml").write_bytes(kit.make_xbrl(note="fixed"))
    run_inbox()
    original, revision = sorted(all_rows(tables.filings), key=lambda r: r.revision_no)
    assert original.filed_on == revision.filed_on == "2026-05-02"
    assert (original.revision_no, revision.revision_no) == (0, 1)
    assert (original.submission, revision.submission) == ("original", "revision")


def test_content_isin_different_from_filename_isin_is_quarantined(db, kit, inbox) -> None:
    (inbox / f"{STEM}.xml").write_bytes(kit.make_xbrl(isin=kit.BETA))
    run_inbox()
    assert only_filing().status == "quarantined" and conflict_fields() == ["isin"]
    assert only_filing().isin == kit.ALPHA  # registered under the claimed, known company


def test_basis_and_period_conflicts_are_all_listed(db, kit, inbox) -> None:
    (inbox / f"{STEM}.xml").write_bytes(
        kit.make_xbrl(basis="Standalone", start="2025-04-01", end="2025-06-30")
    )
    run_inbox()
    assert conflict_fields() == ["period_start", "period_end", "basis"]


def test_a_governance_report_filed_as_results_is_quarantined(db, kit, inbox) -> None:
    """Netweb's `financial_results__Q3FY25` inbox file was an Integrated Governance report."""
    (inbox / f"{STEM}.xml").write_bytes(kit.make_xbrl(family="Governance"))
    run_inbox()
    assert only_filing().status == "quarantined" and conflict_fields() == ["document_kind"]


def test_transcript_whose_title_names_another_quarter_is_quarantined(db, kit, inbox) -> None:
    name = "ALPHAX__earnings_call_transcript__Q1FY27__consolidated.pdf"
    (inbox / name).write_bytes(kit.full_transcript(quarter="Q4 FY26"))
    run_inbox()
    assert only_filing().status == "quarantined" and conflict_fields() == ["quarter"]


def test_unknown_company_is_rejected_not_registered(db, kit, inbox) -> None:
    (inbox / "GHOST__annual_report__FY26__consolidated.pdf").write_bytes(kit.make_pdf(["x"]))
    result = run_inbox()
    assert all_rows(tables.filings) == [] and result.reports == {}
    assert "tracked company" in result.rejected[0].reason


def test_paused_company_files_in_the_inbox_are_still_ingested(db, kit, inbox) -> None:
    with transaction() as conn:
        conn.execute(
            sa.update(tables.entities)
            .where(tables.entities.c.isin == kit.ALPHA)
            .values(status="paused")
        )
    (inbox / f"{STEM}.xml").write_bytes(kit.make_xbrl())
    run_inbox()
    assert len(all_rows(tables.filings)) == 1
    assert kit.ALPHA not in run_ingest(adapters=[]).reports  # but the default run skips it


class Recorder(SourceAdapter):
    """Records the `since` it is asked for and serves nothing."""

    name = "recorder"

    def __init__(self) -> None:
        self.since: list[date | None] = []
        self.began = 0

    def begin_run(self) -> None:
        self.began += 1

    def list_filings(self, isin, since):
        self.since.append(since)
        return []

    def fetch(self, candidate):
        raise AssertionError("nothing to fetch")


def test_backfill_goes_years_back_and_incremental_starts_at_the_last_filing(db, kit, inbox) -> None:
    years = get_settings().history.years_back
    rec = Recorder()
    run_ingest([kit.ALPHA], "backfill", [rec], today=date(2026, 10, 12))
    assert rec.since == [date(2026 - years, 10, 12)] and rec.began == 1
    rec = Recorder()
    run_ingest([kit.ALPHA], "incremental", [rec], today=date(2026, 10, 12))
    assert rec.since == [date(2026 - years, 10, 12)]  # nothing filed yet: same default window
    (inbox / f"{STEM}.xml").write_bytes(kit.make_xbrl(board_date="2026-05-02"))
    run_inbox()
    rec = Recorder()
    run_ingest([kit.ALPHA], "incremental", [rec], today=date(2026, 10, 12))
    assert rec.since == [date(2026, 5, 2)]


class UrlAdapter(SourceAdapter):
    name = "url"

    def __init__(self, candidate: FilingCandidate, data: bytes) -> None:
        self.candidate, self.data, self.fetches = candidate, data, 0

    def list_filings(self, isin, since):
        return [self.candidate]

    def fetch(self, candidate):
        self.fetches += 1
        return self.data


def url_candidate(kit, **kw) -> FilingCandidate:
    base = dict(
        identifier="ALPHAX", isin=kit.ALPHA, source="nse", filing_type="financial_results",
        format=FilingFormat.XBRL, basis=Basis.CONSOLIDATED, period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31), period_type=PeriodType.Q,
        source_url="https://nsearchives.example/x.xml", filed_on="2026-05-02T19:20:54",
    )  # fmt: skip
    return FilingCandidate(**{**base, **kw})


def test_known_source_url_is_not_downloaded_again(db, kit) -> None:
    adapter = UrlAdapter(url_candidate(kit), kit.make_xbrl())
    run_ingest([kit.ALPHA], adapters=[adapter])
    report = run_ingest([kit.ALPHA], adapters=[adapter]).reports[kit.ALPHA]
    assert adapter.fetches == 1 and report.duplicate == 1 and report.new == 0


def test_the_sources_filing_time_wins_over_the_board_meeting_date(db, kit) -> None:
    run_ingest([kit.ALPHA], adapters=[UrlAdapter(url_candidate(kit), kit.make_xbrl())])
    assert only_filing().filed_on == "2026-05-02T19:20:54"


def test_api_listing_that_disagrees_with_the_file_is_quarantined(db, kit) -> None:
    candidate = url_candidate(kit, period_start=date(2025, 10, 1), period_end=date(2025, 12, 31))
    run_ingest([kit.ALPHA], adapters=[UrlAdapter(candidate, kit.make_xbrl())])
    assert only_filing().status == "quarantined"
    assert conflict_fields() == ["period_start", "period_end"]


def test_guessed_transcript_quarter_must_be_confirmed_by_the_title(db, kit) -> None:
    candidate = url_candidate(
        kit,
        filing_type="earnings_call_transcript",
        format=FilingFormat.PDF,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        source_url="https://nsearchives.example/t.pdf",
        period_unverified=True,
    )
    run_ingest([kit.ALPHA], adapters=[UrlAdapter(candidate, kit.full_transcript("Q1 FY27"))])
    assert only_filing().status == "fetched"
    wrong = replace(candidate, source_url="https://nsearchives.example/t2.pdf")
    run_ingest([kit.ALPHA], adapters=[UrlAdapter(wrong, kit.full_transcript("Q4 FY26"))])
    assert sorted(r.status for r in all_rows(tables.filings)) == ["fetched", "quarantined"]


def test_unknown_source_name_is_a_config_error() -> None:
    settings = get_settings()
    bad = settings.model_copy(
        update={"sources": settings.sources.model_copy(update={"enabled": ["carrier_pigeon"]})}
    )
    with pytest.raises(ConfigError, match="carrier_pigeon"):
        build_adapters(bad)


def test_cli_inbox_list_release_roundtrip(db, kit, inbox) -> None:
    runner = CliRunner()
    (inbox / f"{STEM}.xml").write_bytes(kit.make_xbrl(isin=kit.ALPHA_TYPO))
    (inbox / "random.pdf").write_bytes(b"%PDF")
    result = runner.invoke(app, ["ingest", "inbox"])
    assert result.exit_code == 0, result.output
    assert f"{kit.ALPHA}: found 1, new 1" in result.output and "quarantined 1" in result.output
    assert "needs review: random.pdf" in result.output
    listing = runner.invoke(app, ["ingest", "list", "--isin", "ALPHAX"])
    assert "quarantined" in listing.output and "2026-05-02" in listing.output
    filing_id = only_filing().filing_id
    assert "released" in runner.invoke(app, ["ingest", "release", filing_id]).output
    assert only_filing().status == "fetched"
    assert runner.invoke(app, ["ingest", "release", filing_id]).exit_code == 2


def test_cli_run_reports_a_tracked_company_error(db) -> None:
    result = CliRunner().invoke(app, ["ingest", "run", "--isin", "NOPE"])
    assert result.exit_code == 2 and "not a tracked company" in result.output


def test_cli_run_with_only_the_inbox_enabled(db, kit, inbox) -> None:
    (inbox / f"{STEM}.xml").write_bytes(kit.make_xbrl())
    result = CliRunner().invoke(app, ["ingest", "run", "--isin", kit.ALPHA, "--backfill"])
    assert result.exit_code == 0, result.output
    assert len(all_rows(tables.filings)) == 1
    assert Path(only_filing().raw_path).is_file() and registry.pending("fetched")


def test_unknown_period_type_value_is_quarantined_not_a_crash(db, kit, inbox) -> None:
    (inbox / f"{STEM}.xml").write_bytes(kit.make_xbrl(period_type="Annual"))
    result = run_inbox()
    assert result.reports[kit.ALPHA].failed == 0
    assert only_filing().status == "quarantined"
    assert conflict_fields() == ["period_type_unrecognised"]


def test_transcript_without_a_title_is_quarantined_as_quarter_unconfirmed(db, kit, inbox) -> None:
    name = "ALPHAX__earnings_call_transcript__Q1FY27__consolidated.pdf"
    pages = [
        "Cover letter",
        "Management: Welcome everyone.",
        "Moderator: The next question is from the line of Jane Doe from ACME.",
    ]
    (inbox / name).write_bytes(kit.make_pdf(pages))
    run_inbox()
    assert only_filing().status == "quarantined"
    assert conflict_fields() == ["quarter_unconfirmed"]
