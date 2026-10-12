"""One test per M02 acceptance criterion (docs/modules/M02_ingestion.md)."""

import json
import os
from datetime import date
from pathlib import Path

import sqlalchemy as sa

from finrag.core.config import get_settings
from finrag.core.db import tables, transaction
from finrag.core.types import Basis, FilingFormat, PeriodType
from finrag.ingestion import registry
from finrag.ingestion.adapters.base import SourceAdapter
from finrag.ingestion.adapters.local_inbox import LocalInboxAdapter
from finrag.ingestion.http import RateLimiter
from finrag.ingestion.models import FilingCandidate
from finrag.ingestion.raw_store import sha256_of
from finrag.ingestion.registry import DbEntityLookup
from finrag.ingestion.runner import build_adapters, run_inbox, run_ingest

NAME = "ALPHAX__financial_results__Q4FY26__consolidated.xml"


def all_rows(table):
    with transaction() as conn:
        return list(conn.execute(sa.select(table)))


def filings():
    return all_rows(tables.filings)


def reviews(item_type: str):
    return [r for r in all_rows(tables.review_queue) if r.item_type == item_type]


def drop(inbox: Path, name: str, data: bytes) -> Path:
    path = inbox / name
    path.write_bytes(data)
    return path


def test_1_correctly_named_file_gives_one_row_and_a_read_only_raw_copy(db, kit, inbox) -> None:
    drop(inbox, NAME, kit.make_xbrl())
    run_inbox()
    (row,) = filings()
    assert (row.isin, row.filing_type, row.status) == (kit.ALPHA, "financial_results", "fetched")
    raw = Path(row.raw_path)
    assert raw.is_file() and raw.parent.name == "FY26" and raw.name == f"{row.filing_id}.xml"
    assert not os.access(raw, os.W_OK)
    assert not (inbox / NAME).exists()
    assert (get_settings().paths.inbox_processed / NAME).is_file()


def test_2_same_file_again_under_any_name_adds_no_row(db, kit, inbox) -> None:
    data = kit.make_xbrl()
    drop(inbox, NAME, data)
    run_inbox()
    drop(inbox, "something else entirely.xml", data)
    drop(inbox, NAME, data)  # even the very same name
    result = run_inbox()
    assert len(filings()) == 1
    assert sum(r.duplicate for r in result.reports.values()) == 2
    assert [p.name for p in inbox.iterdir()] == ["_processed"]  # duplicates moved out too
    assert len(list(get_settings().paths.inbox_processed.iterdir())) == 3


def test_3_badly_named_non_xbrl_file_goes_to_review_and_makes_no_row(db, kit, inbox) -> None:
    drop(inbox, "Q4 results final.pdf", kit.make_pdf(["hello"]))
    result = run_inbox()
    assert filings() == [] and [r.file_name for r in result.rejected] == ["Q4 results final.pdf"]
    (item,) = reviews("inbox_unrecognised")
    payload = json.loads(item.payload_json)
    assert payload["file_name"] == "Q4 results final.pdf" and payload["reason"]
    assert (inbox / "Q4 results final.pdf").exists()  # left for the user to fix


def test_4_raw_files_are_never_modified(db, kit, inbox) -> None:
    drop(inbox, NAME, kit.make_xbrl())
    drop(inbox, "ALPHAX__earnings_call_transcript__Q1FY27__consolidated.pdf", kit.full_transcript())
    drop(inbox, f"{kit.ALPHA}__annual_report__FY26__consolidated.pdf", kit.make_pdf(["annual"]))
    run_inbox()
    run_inbox()
    rows = filings()
    assert len(rows) == 3
    for row in rows:
        assert sha256_of(Path(row.raw_path).read_bytes()) == row.sha256
        assert row.filing_id == "FIL-" + row.sha256[:12]


class ScriptedAdapter(SourceAdapter):
    """Serves a fixed candidate list per ISIN; fails for ISINs in `broken`."""

    name = "scripted"

    def __init__(self, items: dict[str, list[tuple[FilingCandidate, bytes]]], broken=()) -> None:
        self.items, self.broken, self.fetched = items, set(broken), 0

    def list_filings(self, isin, since):
        if isin in self.broken:
            raise RuntimeError("endpoint changed")
        return [c for c, _ in self.items.get(isin, [])]

    def fetch(self, candidate):
        self.fetched += 1
        for items in self.items.values():
            for cand, data in items:
                if cand.source_url == candidate.source_url:
                    return data
        raise KeyError(candidate)


def scripted_candidate(isin: str, **kw) -> FilingCandidate:
    base = dict(
        identifier=isin, isin=isin, source="scripted", filing_type="financial_results",
        format=FilingFormat.XBRL, basis=Basis.CONSOLIDATED, period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31), period_type=PeriodType.Q,
    )  # fmt: skip
    return FilingCandidate(**{**base, **kw})


def test_5_a_failing_adapter_for_one_isin_does_not_stop_the_others(db, kit) -> None:
    good = scripted_candidate(kit.BETA, source_url="https://example.test/beta.xml")
    adapter = ScriptedAdapter(
        {kit.BETA: [(good, kit.make_xbrl(isin=kit.BETA))]}, broken=[kit.ALPHA]
    )
    result = run_ingest([kit.ALPHA, kit.BETA], adapters=[adapter])
    assert result.reports[kit.ALPHA].failed == 1 and result.reports[kit.BETA].new == 1
    assert [r.isin for r in filings()] == [kit.BETA]
    jobs = {j.isin: j for j in all_rows(tables.jobs)}
    assert jobs[kit.ALPHA].status == "failed" and "endpoint changed" in jobs[kit.ALPHA].error
    assert jobs[kit.BETA].status == "succeeded"
    assert json.loads(jobs[kit.BETA].stats_json)["new"] == 1


def test_5b_one_bad_file_does_not_stop_the_rest_of_the_isin(db, kit) -> None:
    bad = scripted_candidate(kit.ALPHA, source_url="https://example.test/bad.xml")
    good = scripted_candidate(kit.ALPHA, source_url="https://example.test/good.xml")
    adapter = ScriptedAdapter(
        {kit.ALPHA: [(bad, b"not xml"), (good, kit.make_xbrl())]},
    )
    report = run_ingest([kit.ALPHA], adapters=[adapter]).reports[kit.ALPHA]
    assert (report.found, report.new, report.failed) == (2, 1, 1)
    assert "not valid XML" in report.errors[0]
    (job,) = all_rows(tables.jobs)
    assert job.status == "failed" and "not valid XML" in job.error


def test_6_rate_limiter_enforces_requests_per_minute() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limit = get_settings().sources.nse.requests_per_minute
    limiter = RateLimiter(limit, lambda: now[0], sleep)
    for _ in range(limit + 1):
        limiter.wait()
    assert slept == [60.0]


def test_7_with_only_local_inbox_enabled_no_network_adapter_is_built(
    db, kit, inbox, monkeypatch
) -> None:
    assert get_settings().sources.enabled == ["local_inbox"]

    def forbidden(*args, **kwargs):
        raise AssertionError("a network adapter was constructed")

    monkeypatch.setattr("finrag.ingestion.adapters.nse.NseAdapter.__init__", forbidden)
    monkeypatch.setattr("finrag.ingestion.http.HttpxClient.__init__", forbidden)
    monkeypatch.setattr("finrag.ingestion.adapters.bse.BseAdapter.__init__", forbidden)
    adapters = build_adapters(get_settings(), DbEntityLookup())
    assert [type(a) for a in adapters] == [LocalInboxAdapter]
    drop(inbox, NAME, kit.make_xbrl())
    run_ingest()  # the default adapters, the default (watch-list) ISINs
    assert len(filings()) == 1


def test_8_named_transcript_pdf_is_registered_with_the_quarter_discussed(db, kit, inbox) -> None:
    drop(inbox, "ALPHAX__earnings_call_transcript__Q1FY27__consolidated.pdf", kit.full_transcript())
    run_inbox()
    (row,) = filings()
    assert (row.filing_type, row.format, row.status) == (
        "earnings_call_transcript",
        "pdf",
        "fetched",
    )
    assert (row.period_type, row.period_start, row.period_end) == ("Q", "2026-04-01", "2026-06-30")
    assert row.filed_on is None


def test_9_link_only_cover_letter_is_queued_and_not_registered(db, kit, inbox) -> None:
    drop(
        inbox, "ALPHAX__earnings_call_transcript__Q1FY27__consolidated.pdf", kit.link_only_letter()
    )
    result = run_inbox()
    assert filings() == [] and result.reports[kit.ALPHA].link_only == 1
    (item,) = reviews("transcript_link_only")
    payload = json.loads(item.payload_json)
    assert payload["page_count"] == 1 and payload["subject_quarter_verified"] is False
    assert "https://www.alpha.example/investors/transcripts" in payload["links"]
    assert any("website" in line for line in payload["link_text"])
    assert payload["subject_quarter"] == "Q1 FY26"  # the letter names the wrong quarter
    assert Path(payload["raw_path"]).is_file()  # the raw file is still kept


def test_9b_full_transcript_behind_a_short_cover_letter_is_not_flagged(db, kit, inbox) -> None:
    drop(inbox, "ALPHAX__earnings_call_transcript__Q1FY27__consolidated.pdf", kit.full_transcript())
    run_inbox()
    assert reviews("transcript_link_only") == [] and len(filings()) == 1


def test_10_content_that_conflicts_with_the_filename_is_quarantined(db, kit, inbox) -> None:
    original = kit.make_xbrl(
        isin=kit.ALPHA_TYPO, start="2025-10-01", end="2026-03-31", period_type="Half Yearly"
    )
    revision = kit.make_xbrl(note="corrected")
    stem = "ALPHAX__financial_results__Q4FY26__consolidated"
    drop(inbox, f"{stem}.xml", original)
    drop(inbox, f"{stem}__rev1.xml", revision)
    result = run_inbox()
    report = result.reports[kit.ALPHA]
    assert (report.new, report.quarantined, report.failed) == (2, 1, 0)
    by_name = {Path(r.raw_path).name: r for r in filings()}
    quarantined = next(r for r in by_name.values() if r.status == "quarantined")
    rev = next(r for r in by_name.values() if r.status == "fetched")
    assert quarantined.submission == "original" and quarantined.sha256 == sha256_of(original)
    assert (rev.submission, rev.revision_no) == ("revision", 1)
    assert Path(quarantined.raw_path).read_bytes() == original  # evidence kept untouched
    (item,) = reviews("filing_conflict")
    payload = json.loads(item.payload_json)
    assert [c["field"] for c in payload["conflicts"]] == ["isin", "period_start", "period_type"]
    assert payload["superseded_by"] == [rev.filing_id]
    assert payload["file_name"] == f"{stem}.xml"
    assert [r.filing_id for r in registry.pending("fetched")] == [rev.filing_id]
    registry.release(quarantined.filing_id)
    assert {r.filing_id for r in registry.pending("fetched")} == {
        quarantined.filing_id, rev.filing_id,
    }  # fmt: skip
