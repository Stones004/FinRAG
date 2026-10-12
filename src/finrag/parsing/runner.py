"""Run parsing: filings with status `fetched` -> parsed_facts, page text and review items."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.jobs import finish_job, start_job
from finrag.core.logging import bind, get_logger
from finrag.core.types import Basis, FilingFormat, FilingStatus, JobStatus, decimal_to_text
from finrag.ingestion import registry
from finrag.ingestion.raw_store import sha256_of
from finrag.parsing.models import ParsedFact, TextFact, dimensions_json
from finrag.parsing.ocr.base import OcrBackend
from finrag.parsing.ocr.factory import get_backend
from finrag.parsing.page_classifier import classify_pages
from finrag.parsing.pdf_tables import TablesResult, extract_statement_tables
from finrag.parsing.pdf_text import extract_pdf_text, write_pages_jsonl
from finrag.parsing.sections import SectionPatterns, load_section_patterns
from finrag.parsing.text_quality import Vocabulary, load_vocabulary
from finrag.parsing.xbrl_parser import parse_xbrl

JOB_TYPE = "parse"
ITEM_LOW_CONFIDENCE = "low_confidence_table"
FILING_TYPE_RESULTS = "financial_results"
PAGES_FILE = "pages.jsonl"
TEXT_FACTS_FILE = "text_facts.jsonl"
_BATCH = 500

log = get_logger("parsing")


class ParseError(Exception):
    """Raised when one filing cannot be parsed; the message says why."""


@dataclass
class ParseReport:
    """Outcome for one filing."""

    filing_id: str
    status: str  # parsed | failed
    stats: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class _Tools:
    """Objects built once per run and shared by every filing."""

    settings: Settings
    vocabulary: Vocabulary
    patterns: SectionPatterns
    backend: OcrBackend


def _tools(settings: Settings, backend: OcrBackend | None) -> _Tools:
    """Load vocabulary, section patterns and the OCR backend named in settings."""
    return _Tools(
        settings,
        load_vocabulary(settings),
        load_section_patterns(settings),
        backend or get_backend(settings),
    )


def _row(filing_id: str, fact: ParsedFact) -> dict[str, Any]:
    """ParsedFact -> parsed_facts column values."""
    return {
        "filing_id": filing_id,
        "concept_raw": fact.concept_raw,
        "label_raw": fact.label_raw,
        "period_start": fact.period_start.isoformat() if fact.period_start else None,
        "period_end": fact.period_end.isoformat() if fact.period_end else None,
        "basis": fact.basis,
        "value": None if fact.value is None else decimal_to_text(fact.value),
        "unit_raw": fact.unit_raw,
        "scale": fact.scale,
        "decimals": fact.decimals,
        "source_loc": fact.source_loc,
        "dimensions_json": dimensions_json(fact.dimensions),
        "statement_type": fact.statement_type,
        "extractor": fact.extractor,
        "ocr_backend": fact.ocr_backend,
        "confidence": fact.confidence,
    }


def _write_facts(filing_id: str, facts: Sequence[ParsedFact], engine: Engine | None) -> None:
    """Replace the filing's parsed_facts with these facts (one transaction)."""
    rows = [_row(filing_id, f) for f in facts]
    with transaction(engine) as conn:
        conn.execute(
            sa.delete(tables.parsed_facts).where(tables.parsed_facts.c.filing_id == filing_id)
        )
        for start in range(0, len(rows), _BATCH):
            conn.execute(sa.insert(tables.parsed_facts), rows[start : start + _BATCH])


def _queue_tables(filing_id: str, result: TablesResult, engine: Engine | None) -> None:
    """Low-confidence and unreadable tables -> review_queue items (one open item per page)."""
    with transaction(engine) as conn:
        for item in result.review_items:
            payload = {"filing_id": filing_id, **item}
            registry.queue_review(
                conn, ITEM_LOW_CONFIDENCE, payload, {"filing_id": filing_id, "page": item["page"]}
            )


def _write_text_facts(path: Path, facts: Sequence[TextFact]) -> None:
    """Write the non-numeric XBRL facts as JSON lines (replaces an earlier file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for fact in facts:
            handle.write(json.dumps(fact.as_json(), ensure_ascii=False) + "\n")


def _parse_xbrl_filing(
    filing: sa.Row, data: bytes, out_dir: Path, engine: Engine | None
) -> dict[str, Any]:
    """XBRL filing -> parsed_facts + text_facts.jsonl; returns the counts."""
    result = parse_xbrl(data, filing.basis or Basis.UNKNOWN)
    _write_facts(filing.filing_id, result.facts, engine)
    _write_text_facts(out_dir / TEXT_FACTS_FILE, result.text_facts)
    return {
        "numeric_facts": len(result.facts),
        "dimensioned_facts": sum(1 for f in result.facts if f.dimensions),
        "text_facts": len(result.text_facts),
    }


def _parse_pdf_filing(
    filing: sa.Row, data: bytes, out_dir: Path, tools: _Tools, engine: Engine | None
) -> dict[str, Any]:
    """PDF filing -> pages.jsonl (+ statement-table facts for results PDFs); returns the counts."""
    text = extract_pdf_text(
        data, filing.filing_type, tools.backend, tools.vocabulary, tools.settings
    )
    write_pages_jsonl(out_dir / PAGES_FILE, text.pages)
    stats: dict[str, Any] = text.stats()
    facts: list[ParsedFact] = []
    if filing.filing_type == FILING_TYPE_RESULTS:
        labels = classify_pages(text.pages, tools.patterns, tools.settings)
        tables_result = extract_statement_tables(
            data,
            text.pages,
            labels,
            tools.patterns,
            Basis(filing.basis or Basis.UNKNOWN),
            tools.settings,
        )
        facts = tables_result.facts
        _queue_tables(filing.filing_id, tables_result, engine)
        stats["statement_pages"] = [lb.page for lb in labels if lb.statement_type]
        stats["table_pages_read"] = tables_result.pages_read
        stats["unit_inherited"] = {
            lb.page: lb.unit_from_page for lb in labels if lb.unit_from_page is not None
        }
        stats["table_pages_unread"] = tables_result.pages_unread
        stats["review_items"] = len(tables_result.review_items)
    _write_facts(filing.filing_id, facts, engine)
    stats["numeric_facts"] = len(facts)
    return stats


def parse_filing(filing: sa.Row, tools: _Tools, engine: Engine | None = None) -> ParseReport:
    """One filing row -> parsed output, status parsed or failed, and a jobs row with counts."""
    job_id = start_job(JOB_TYPE, filing.isin, engine)
    report = ParseReport(filing.filing_id, FilingStatus.PARSED.value)
    try:
        with bind(isin=filing.isin, filing_id=filing.filing_id):
            raw = Path(filing.raw_path)
            if not raw.exists():
                raise ParseError(f"raw file is missing: {raw}")
            data = raw.read_bytes()
            if sha256_of(data) != filing.sha256:
                raise ParseError(f"raw file {raw} no longer matches its recorded sha256")
            out_dir = tools.settings.paths.processed / filing.filing_id
            if filing.format == FilingFormat.XBRL.value:
                report.stats = _parse_xbrl_filing(filing, data, out_dir, engine)
            else:
                report.stats = _parse_pdf_filing(filing, data, out_dir, tools, engine)
        registry.set_status(filing.filing_id, FilingStatus.PARSED, None, engine)
        finish_job(job_id, JobStatus.SUCCEEDED, report.stats, None, engine)
    except Exception as exc:  # one bad filing must not stop the run; the error is recorded
        report.status = FilingStatus.FAILED.value
        report.error = f"{type(exc).__name__}: {exc}"
        registry.set_status(filing.filing_id, FilingStatus.FAILED, report.error, engine)
        finish_job(job_id, JobStatus.FAILED, report.stats, report.error, engine)
        log.error("parse failed: %s", report.error, extra={"event": "parse_failed"})
    return report


def _select(filing_id: str | None, engine: Engine | None) -> list[sa.Row]:
    """Filings to parse: all `fetched` ones, or the one named (not quarantined or normalized)."""
    if filing_id is None:
        return registry.pending(FilingStatus.FETCHED, engine)
    with transaction(engine) as conn:
        row = conn.execute(
            sa.select(tables.filings).where(tables.filings.c.filing_id == filing_id)
        ).first()
    if row is None:
        raise ParseError(f"unknown filing: {filing_id}")
    if row.status == FilingStatus.QUARANTINED.value:
        raise ParseError(f"{filing_id} is quarantined; release it first (finrag ingest release)")
    if row.status == FilingStatus.NORMALIZED.value:
        raise ParseError(f"{filing_id} is already normalized; its parsed facts are in use")
    return [row]


def run_parse(
    filing_id: str | None = None,
    engine: Engine | None = None,
    settings: Settings | None = None,
    backend: OcrBackend | None = None,
) -> list[ParseReport]:
    """Parse every `fetched` filing (or the one named) -> one ParseReport each."""
    settings = settings or get_settings()
    filings = _select(filing_id, engine)
    if not filings:
        return []
    tools = _tools(settings, backend)
    return [parse_filing(filing, tools, engine) for filing in filings]
