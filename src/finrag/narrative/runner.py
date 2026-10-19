"""Index filings: section -> chunk -> write `chunks` -> skip summary -> BM25 and vector indexes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import ConfigError, Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.fiscal import fiscal_year_of, fy_label
from finrag.core.jobs import finish_job, start_job
from finrag.core.logging import bind, get_logger
from finrag.core.types import Basis, FilingStatus, JobStatus
from finrag.narrative import store
from finrag.narrative.chunker import (
    chunk_results_slices,
    chunk_text_block,
    chunk_transcript_unit,
    repeated_edge_lines,
)
from finrag.narrative.embedder import Embedder, SentenceTransformerEmbedder
from finrag.narrative.indexer import rebuild_isin
from finrag.narrative.models import (
    REASON_DEFERRED,
    REASON_NOT_INDEXED,
    REASON_UNKNOWN,
    SOURCE_RESULTS,
    SOURCE_TRANSCRIPT,
    DraftChunk,
    PageSlice,
    SkippedSpan,
    SkipSummary,
)
from finrag.narrative.patterns import NarrativePatterns, load_narrative_patterns
from finrag.narrative.sectioner import PageInput, section_results_pdf
from finrag.narrative.textblocks import read_text_blocks
from finrag.narrative.transcript_sectioner import section_transcript
from finrag.parsing.page_classifier import classify_pages
from finrag.parsing.pdf_text import PageText, read_pages_jsonl
from finrag.parsing.runner import PAGES_FILE, TEXT_FACTS_FILE
from finrag.parsing.sections import load_section_patterns

JOB_TYPE = "narrative_index"
SUMMARY_FILE = "narrative_summary.json"
TYPE_RESULTS = "financial_results"
TYPE_TRANSCRIPT = "earnings_call_transcript"
TYPE_ANNUAL = "annual_report"
INDEXABLE = (FilingStatus.PARSED.value, FilingStatus.NORMALIZED.value)

log = get_logger("narrative")


@dataclass
class IndexReport:
    """Outcome for one filing."""

    filing_id: str
    isin: str
    status: str  # indexed | unchanged | superseded | deferred | failed
    chunks: int = 0
    detail: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


def _filings(isin: str | None, filing_id: str | None, engine: Engine | None) -> list[sa.Row]:
    """Parsed or normalized filings of the narrative types, oldest first."""
    f = tables.filings
    query = sa.select(f).where(
        f.c.status.in_(INDEXABLE),
        f.c.filing_type.in_((TYPE_RESULTS, TYPE_TRANSCRIPT, TYPE_ANNUAL)),
    )
    if isin:
        query = query.where(f.c.isin == isin)
    if filing_id:
        query = query.where(f.c.filing_id == filing_id)
    with transaction(engine) as conn:
        return list(conn.execute(query.order_by(f.c.isin, f.c.period_end, f.c.filing_id)))


def _rank(row: sa.Row) -> tuple[str, int, str]:
    """Order of versions of one filing: filing date, revision number, registration time."""
    return ((row.filed_on or "")[:10], row.revision_no or 0, row.created_at or "")


def is_superseded(conn: Any, filing: sa.Row) -> bool:
    """True when a later version of the same filing (revision) is also indexable."""
    f = tables.filings
    others = conn.execute(
        sa.select(f).where(
            f.c.isin == filing.isin,
            f.c.filing_type == filing.filing_type,
            f.c.format == filing.format,
            f.c.basis == filing.basis,
            f.c.period_end == filing.period_end,
            f.c.filing_id != filing.filing_id,
            f.c.status.in_(INDEXABLE),
        )
    ).all()
    return any(_rank(other) > _rank(filing) for other in others)


def _fiscal_year(filing: sa.Row) -> str | None:
    """Fiscal-year label of the filing's period end."""
    if not filing.period_end:
        return None
    return fy_label(fiscal_year_of(date.fromisoformat(filing.period_end)))


def _meta(filing: sa.Row) -> dict[str, Any]:
    """Metadata every chunk of a filing carries."""
    return {"basis": filing.basis}


def results_pdf_inputs(pages: list[dict[str, Any]], settings: Settings) -> list[PageInput]:
    """pages.jsonl records -> sectioner input, with the M03 classifier's statement labels."""
    texts = [
        PageText(
            page=r["page"],
            text=r["text"],
            method=r["method"],
            ocr_backend=r.get("ocr_backend"),
            char_count=r["char_count"],
            quality=r["quality"],
            low_quality=r["low_quality"],
        )
        for r in pages
    ]
    labels = classify_pages(texts, load_section_patterns(settings), settings)
    return [
        PageInput(
            t.page,
            t.text,
            label.statement_type,
            t.quality,
            None if label.basis is Basis.UNKNOWN else label.basis.value,
        )
        for t, label in zip(texts, labels, strict=True)
    ]


def analyse_results_pdf(
    filing_dir: Path, settings: Settings, patterns: NarrativePatterns
) -> tuple[list[PageSlice], SkipSummary, list[PageInput]]:
    """A parsed results PDF -> (kept slices, summary, all pages as the sectioner saw them)."""
    pages = read_pages_jsonl(filing_dir / PAGES_FILE)
    inputs = results_pdf_inputs(pages, settings)
    slices, summary = section_results_pdf(inputs, patterns.results)
    return slices, summary, inputs


def _results_pdf_drafts(
    filing: sa.Row, settings: Settings, patterns: NarrativePatterns
) -> tuple[list[DraftChunk], SkipSummary]:
    """Results PDF -> chunks of its notes and auditor's-report pages."""
    slices, summary, inputs = analyse_results_pdf(
        settings.paths.processed / filing.filing_id, settings, patterns
    )
    furniture = repeated_edge_lines([p.text for p in inputs])
    drafts = chunk_results_slices(slices, furniture, settings.narrative, _meta(filing))
    summary.extra["chunks"] = len(drafts)
    return drafts, summary


def _text_block_drafts(
    filing: sa.Row, settings: Settings, patterns: NarrativePatterns
) -> tuple[list[DraftChunk], SkipSummary]:
    """XBRL results filing -> chunks of its indexed text blocks (None found is not an error)."""
    path = settings.paths.processed / filing.filing_id / TEXT_FACTS_FILE
    blocks = read_text_blocks(path, patterns.text_blocks)
    drafts: list[DraftChunk] = []
    for block in blocks:
        drafts.extend(chunk_text_block(block, settings.narrative, _meta(filing)))
    summary = SkipSummary()
    summary.extra["blocks"] = [{"concept": b.concept, "chars": len(b.text)} for b in blocks]
    summary.extra["chunks"] = len(drafts)
    absent = sorted(set(patterns.text_blocks) - {b.concept for b in blocks})
    summary.extra["absent_concepts"] = absent
    return drafts, summary


def _span_pages(pages: list[int], reason: str, detail: str) -> list[SkippedSpan]:
    """Page numbers -> skipped spans of consecutive pages."""
    spans: list[SkippedSpan] = []
    for page in sorted(pages):
        if spans and spans[-1].end_page == page - 1:
            spans[-1] = SkippedSpan(spans[-1].start_page, page, reason, detail)
        else:
            spans.append(SkippedSpan(page, page, reason, detail))
    return spans


def _transcript_drafts(
    filing: sa.Row, settings: Settings, patterns: NarrativePatterns
) -> tuple[list[DraftChunk], SkipSummary]:
    """Transcript -> chunks (prepared remarks, Q&A units, closing remarks)."""
    pages = read_pages_jsonl(settings.paths.processed / filing.filing_id / PAGES_FILE)
    result = section_transcript(pages, patterns.transcript)
    drafts: list[DraftChunk] = []
    for number, unit in enumerate(result.units):
        meta = {**_meta(filing), "unit": number}
        drafts.extend(chunk_transcript_unit(unit, settings.narrative, meta))
    summary = result.summary
    cover = list(summary.extra.get("cover_pages", []))
    kept = {p for u in result.units for p in range(u.start_page, u.end_page + 1)}
    rest = [p["page"] for p in pages if p["page"] not in kept and p["page"] not in cover]
    summary.skipped = [
        *_span_pages(cover, REASON_NOT_INDEXED, "cover letter / title page"),
        *_span_pages(rest, REASON_UNKNOWN, "no indexed turn on this page"),
    ]
    summary.pages_kept = summary.pages_total - summary.pages_skipped
    summary.extra["chunks"] = len(drafts)
    summary.extra["units"] = len(result.units)
    return drafts, summary


def _drafts(
    filing: sa.Row, settings: Settings, patterns: NarrativePatterns
) -> tuple[list[DraftChunk], SkipSummary, str]:
    """Filing -> (chunk drafts, summary, source_type)."""
    if filing.filing_type == TYPE_TRANSCRIPT:
        drafts, summary = _transcript_drafts(filing, settings, patterns)
        return drafts, summary, SOURCE_TRANSCRIPT
    if filing.format == "xbrl":
        drafts, summary = _text_block_drafts(filing, settings, patterns)
        return drafts, summary, SOURCE_RESULTS
    drafts, summary = _results_pdf_drafts(filing, settings, patterns)
    return drafts, summary, SOURCE_RESULTS


def _write_summary(filing: sa.Row, summary: SkipSummary, settings: Settings, source: str) -> None:
    """Write processed/<filing_id>/narrative_summary.json."""
    record = {
        "filing_id": filing.filing_id,
        "filing_type": filing.filing_type,
        "source_type": source,
        **summary.as_json(settings.narrative.log_skipped_sections),
    }
    path = settings.paths.processed / filing.filing_id / SUMMARY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")


def index_filing(
    filing: sa.Row,
    settings: Settings,
    patterns: NarrativePatterns,
    reindex: bool,
    engine: Engine | None,
) -> IndexReport:
    """One filing -> chunks written (or a reason it was left alone)."""
    report = IndexReport(filing.filing_id, filing.isin, "indexed")
    if filing.filing_type == TYPE_ANNUAL:
        if settings.narrative.index_annual_reports:
            raise ConfigError(
                "narrative.index_annual_reports is true but annual-report indexing is deferred "
                "and not built (docs/modules/M08_narrative.md)"
            )
        report.status, report.detail = "deferred", REASON_DEFERRED
        return report
    with transaction(engine) as conn:
        if is_superseded(conn, filing):
            removed = store.delete_for_filing(conn, filing.filing_id)
            report.status = "superseded"
            report.detail = f"a later version exists; {removed} chunks removed"
            return report
        existing = store.count_for_filing(conn, filing.filing_id)
    if existing and not reindex:
        report.status, report.chunks = "unchanged", existing
        return report
    drafts, summary, source = _drafts(filing, settings, patterns)
    with transaction(engine) as conn:
        store.replace_for_filing(conn, filing, source, _fiscal_year(filing), drafts)
    _write_summary(filing, summary, settings, source)
    report.chunks = len(drafts)
    report.stats = {k: v for k, v in summary.extra.items() if k in ("units", "chunks")}
    report.stats.update(pages_kept=summary.pages_kept, pages_total=summary.pages_total)
    return report


def run_index(
    isin: str | None = None,
    filing_id: str | None = None,
    reindex: bool = False,
    rebuild_vectors: bool = False,
    engine: Engine | None = None,
    settings: Settings | None = None,
    embedder: Embedder | None = None,
) -> list[IndexReport]:
    """Index every parsed filing that has no chunks yet (or those named) -> one report each.

    The BM25 and vector indexes of every ISIN whose chunks changed are then refreshed;
    `rebuild_vectors` re-embeds every chunk (use it after changing the embedding model).
    """
    settings = settings or get_settings()
    patterns = load_narrative_patterns()
    filings = _filings(isin, filing_id, engine)
    reports: list[IndexReport] = []
    touched: set[str] = set()
    for filing in filings:
        job_id = start_job(JOB_TYPE, filing.isin, engine)
        try:
            with bind(isin=filing.isin, filing_id=filing.filing_id):
                report = index_filing(filing, settings, patterns, reindex, engine)
            finish_job(
                job_id, JobStatus.SUCCEEDED, {"status": report.status, **report.stats}, None, engine
            )
        except ConfigError:
            finish_job(job_id, JobStatus.FAILED, {}, "configuration error", engine)
            raise
        except Exception as exc:  # one bad filing must not stop the run
            report = IndexReport(
                filing.filing_id, filing.isin, "failed", detail=f"{type(exc).__name__}: {exc}"
            )
            finish_job(job_id, JobStatus.FAILED, {}, report.detail, engine)
            log.error("narrative index failed: %s", report.detail, extra={"event": "index_failed"})
        if report.status in ("indexed", "superseded"):
            touched.add(filing.isin)
        reports.append(report)
    if rebuild_vectors:
        touched |= {f.isin for f in filings}
    if touched:
        embedder = embedder or SentenceTransformerEmbedder(settings.narrative)
        for item in sorted(touched):
            stats = rebuild_isin(item, embedder, settings, engine, force=rebuild_vectors)
            log.info("indexes rebuilt", extra={"event": "indexes_rebuilt", "isin": item, **stats})
    return reports
