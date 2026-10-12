"""Run ingestion: list, fetch, check, store and register filings; one jobs row per ISIN."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import ConfigError, Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.jobs import finish_job, start_job
from finrag.core.logging import get_logger
from finrag.core.types import FilingFormat, JobStatus
from finrag.ingestion import registry
from finrag.ingestion.adapters.base import SourceAdapter
from finrag.ingestion.adapters.local_inbox import FILING_TYPE_TRANSCRIPT, LocalInboxAdapter
from finrag.ingestion.conflicts import transcript_conflicts, xbrl_conflicts
from finrag.ingestion.models import Conflict, FilingCandidate, IngestReport, Rejected
from finrag.ingestion.raw_store import sha256_of, store_raw
from finrag.ingestion.registry import DbEntityLookup, RawFile
from finrag.ingestion.transcript_check import check_transcript
from finrag.ingestion.xbrl_meta import parse_xbrl_meta

Mode = Literal["incremental", "backfill"]
JOB_TYPE = "ingest"
NEW, DUPLICATE, QUARANTINED, LINK_ONLY = "new", "duplicate", "quarantined", "link_only"

log = get_logger("ingestion")


class IngestError(Exception):
    """Raised when one candidate cannot be ingested; the message says why."""


@dataclass
class RunResult:
    """Reports per ISIN, plus inbox files that could not be interpreted."""

    reports: dict[str, IngestReport] = field(default_factory=dict)
    rejected: list[Rejected] = field(default_factory=list)


def build_adapters(
    settings: Settings | None = None, lookup: DbEntityLookup | None = None
) -> list[SourceAdapter]:
    """settings.sources.enabled -> adapter instances; only enabled adapters are constructed."""
    settings = settings or get_settings()
    lookup = lookup or DbEntityLookup()
    adapters: list[SourceAdapter] = []
    for name in settings.sources.enabled:
        if name == "local_inbox":
            adapters.append(LocalInboxAdapter(lookup, settings))
        elif name == "nse":
            from finrag.ingestion.adapters.nse import NseAdapter
            from finrag.ingestion.http import HttpxClient

            adapters.append(NseAdapter(settings, HttpxClient(settings.sources.nse), lookup))
        elif name == "bse":
            from finrag.ingestion.adapters.bse import BseAdapter

            adapters.append(BseAdapter())
        else:
            raise ConfigError(f"sources.enabled: unknown source '{name}'")
    return adapters


def _since_for(isin: str, mode: Mode, settings: Settings, today: date, engine: Engine | None):
    """ISIN + mode -> earliest date to list from (backfill: years_back, else last filing)."""
    backfill = date(today.year - settings.history.years_back, today.month, min(today.day, 28))
    if mode == "backfill":
        return backfill
    with transaction(engine) as conn:
        latest = conn.execute(
            sa.select(sa.func.max(tables.filings.c.filed_on)).where(tables.filings.c.isin == isin)
        ).scalar()
    return date.fromisoformat(latest[:10]) if latest else backfill


def _move_to_processed(path: Path, sha: str, settings: Settings) -> None:
    """Move a handled inbox file into inbox/_processed/ (name made unique if taken)."""
    target_dir = settings.paths.inbox_processed
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}__{sha[:8]}{path.suffix}"
    shutil.move(str(path), str(target))


def _xbrl_checks(
    candidate: FilingCandidate, data: bytes
) -> tuple[FilingCandidate, str | None, list[Conflict]]:
    """XBRL bytes -> (candidate with filed_on, taxonomy family, conflicts).

    The source's own filing time wins; otherwise `filed_on` is the board-meeting date in the file.
    """
    meta = parse_xbrl_meta(data)
    filed_on = candidate.filed_on or (
        meta.board_meeting_date.isoformat() if meta.board_meeting_date else None
    )
    return (
        replace(candidate, filed_on=filed_on),
        meta.taxonomy_family,
        xbrl_conflicts(candidate, meta),
    )


def _finish_inbox_file(candidate: FilingCandidate, sha: str, settings: Settings) -> None:
    """Move a handled inbox file out of the inbox; a failed move is logged, not fatal."""
    if candidate.local_path is None:
        return
    try:
        _move_to_processed(candidate.local_path, sha, settings)
    except OSError as exc:
        log.warning(
            "could not move inbox file",
            extra={"event": "move_failed", "fields": {"file": str(candidate.local_path)}},
        )
        del exc


def process_candidate(
    adapter: SourceAdapter,
    candidate: FilingCandidate,
    lookup: DbEntityLookup,
    engine: Engine | None = None,
    settings: Settings | None = None,
) -> str:
    """One candidate -> NEW | DUPLICATE | QUARANTINED | LINK_ONLY (IngestError on failure)."""
    settings = settings or get_settings()
    isin = candidate.isin or lookup.isin_for(candidate.identifier)
    if isin is None:
        raise IngestError(f"'{candidate.identifier}' is not a tracked company")
    candidate = replace(candidate, isin=isin)
    if candidate.source_url and registry.known_url(candidate.source_url, engine):
        return DUPLICATE
    try:
        data = adapter.fetch(candidate)
        sha = sha256_of(data)
        with transaction(engine) as conn:
            known = registry.filing_by_sha(conn, sha) is not None
        if known:
            _finish_inbox_file(candidate, sha, settings)
            return DUPLICATE
        conflicts: list[Conflict] = []
        family: str | None = None
        if candidate.format is FilingFormat.XBRL:
            candidate, family, conflicts = _xbrl_checks(candidate, data)
        elif candidate.filing_type == FILING_TYPE_TRANSCRIPT:
            check = check_transcript(data, settings)
            if check.is_link_only:
                return _handle_link_only(candidate, data, check, engine, settings)
            conflicts = transcript_conflicts(candidate, check)
        filing_id, path, _ = store_raw(data, candidate, isin, settings)
        _, is_new = registry.register(
            candidate, RawFile(filing_id, path, sha), conflicts, family, engine
        )
    except Exception as exc:  # any failure on one file is reported, never raised past the runner
        raise IngestError(f"{candidate.local_path or candidate.download_url}: {exc}") from exc
    _finish_inbox_file(candidate, sha, settings)
    if not is_new:
        return DUPLICATE
    return QUARANTINED if conflicts else NEW


def _handle_link_only(candidate, data, check, engine, settings) -> str:
    """Keep the raw file, queue transcript_link_only, register nothing."""
    isin = candidate.isin
    _, path, sha = store_raw(data, candidate, isin, settings)
    file_name = candidate.local_path.name if candidate.local_path else path.name
    registry.queue_link_only(
        {
            "sha256": sha,
            "file_name": file_name,
            "isin": isin,
            "raw_path": str(path),
            "page_count": check.page_count,
            "links": list(check.links),
            "link_text": list(check.link_text),
            "subject_quarter": check.subject_quarter,
            "subject_quarter_verified": False,
        },
        engine,
    )
    _finish_inbox_file(candidate, sha, settings)
    return LINK_ONLY


def _tally(report: IngestReport, outcome: str) -> None:
    """Count one outcome in the report."""
    if outcome == DUPLICATE:
        report.duplicate += 1
    elif outcome == LINK_ONLY:
        report.link_only += 1
    else:
        report.new += 1
        if outcome == QUARANTINED:
            report.quarantined += 1


def _ingest_isin(
    isin: str,
    adapters: Sequence[SourceAdapter],
    mode: Mode,
    lookup: DbEntityLookup,
    engine: Engine | None,
    settings: Settings,
    today: date,
) -> IngestReport:
    """All adapters for one ISIN -> IngestReport; every failure is recorded, none is raised."""
    report = IngestReport()
    job_id = start_job(JOB_TYPE, isin, engine)
    since = _since_for(isin, mode, settings, today, engine)
    for adapter in adapters:
        try:
            candidates = adapter.list_filings(isin, since)
        except Exception as exc:  # one broken source must not stop the others
            report.failed += 1
            report.errors.append(f"{adapter.name}: listing failed: {exc}")
            log.warning("listing failed", extra={"event": "list_failed", "fields": {"isin": isin}})
            continue
        report.found += len(candidates)
        for candidate in candidates:
            try:
                _tally(report, process_candidate(adapter, candidate, lookup, engine, settings))
            except Exception as exc:
                report.failed += 1
                report.errors.append(f"{adapter.name}: {exc}")
    finish_job(
        job_id,
        JobStatus.FAILED if report.failed else JobStatus.SUCCEEDED,
        report.stats(),
        "; ".join(report.errors) or None,
        engine,
    )
    return report


def run_ingest(
    isins: Sequence[str] | None = None,
    mode: Mode = "incremental",
    adapters: Sequence[SourceAdapter] | None = None,
    engine: Engine | None = None,
    settings: Settings | None = None,
    today: date | None = None,
) -> RunResult:
    """ISINs (default: active watch list) -> RunResult; each ISIN is its own job."""
    from finrag.watchlist.registry import today_in_app_tz

    settings = settings or get_settings()
    lookup = DbEntityLookup(engine)
    adapters = list(adapters) if adapters is not None else build_adapters(settings, lookup)
    isins = list(isins) if isins is not None else lookup.tracked_isins()
    day = today or today_in_app_tz(settings)
    for adapter in adapters:
        adapter.begin_run()
    result = RunResult()
    for isin in isins:
        result.reports[isin] = _ingest_isin(isin, adapters, mode, lookup, engine, settings, day)
    for adapter in adapters:
        for item in adapter.rejected():
            registry.queue_unrecognised(item.file_name, item.reason, engine)
            result.rejected.append(item)
    return result


def run_inbox(
    engine: Engine | None = None, settings: Settings | None = None, today: date | None = None
) -> RunResult:
    """Process everything in the inbox, for any known ISIN (tracked or paused)."""
    settings = settings or get_settings()
    inbox = LocalInboxAdapter(DbEntityLookup(engine), settings)
    isins = sorted({c.isin for c in inbox.all_candidates() if c.isin})
    return run_ingest(isins, "incremental", [inbox], engine, settings, today)
