"""Run normalization: `parsed` filings -> canonical candidates for M05, plus review items."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.jobs import finish_job, start_job
from finrag.core.logging import bind, get_logger
from finrag.core.types import Basis, FilingFormat, FilingStatus, JobStatus
from finrag.ingestion import registry
from finrag.normalize import review_queue as queue
from finrag.normalize.basis import resolve_basis
from finrag.normalize.mapper import Known, Mapped, Mapper, NotApplicable, Unmapped
from finrag.normalize.models import CandidateWriter, CanonicalCandidate
from finrag.normalize.periods import ANCHOR_DAY_BEFORE_START, ANCHOR_END, classify
from finrag.normalize.taxonomy import Concept, Taxonomy, load_taxonomy
from finrag.normalize.units import to_canonical
from finrag.parsing.models import EXTRACTOR_XBRL, ParsedFact

JOB_TYPE = "normalize"

ISSUE_NO_NAME = "no_name"
ISSUE_DUPLICATE_MISMATCH = "duplicate_mismatch"
ISSUE_DUPLICATE_CANDIDATES = "duplicate_candidates"
ISSUE_LOW_CONFIDENCE = "low_confidence_sole_source"

log = get_logger("normalize")


class NormalizeError(Exception):
    """Raised when a normalization run cannot start; the message says why."""


@dataclass(frozen=True)
class FilingContext:
    """What the normalizer needs to know about one filing."""

    filing_id: str
    isin: str
    family: str  # taxonomy family; "" for PDF filings (labels only)
    sector: str
    basis: str | None
    filed_on: str | None


@dataclass(frozen=True)
class FactRow:
    """One parsed_facts row as the normalizer sees it."""

    id: int
    fact: ParsedFact
    dimensioned: bool


@dataclass(frozen=True)
class UnmappedRequest:
    """An unmapped QName or label to queue."""

    outcome: Unmapped
    example: dict[str, Any]


@dataclass(frozen=True)
class IssueRequest:
    """A fact that cannot be normalized, to queue."""

    reason: str
    key: str
    detail: dict[str, Any]


@dataclass
class NormalizeOutcome:
    """Result of normalizing one filing's facts (no database writes)."""

    candidates: list[CanonicalCandidate] = field(default_factory=list)
    unmapped: list[UnmappedRequest] = field(default_factory=list)
    issues: list[IssueRequest] = field(default_factory=list)
    basis_unknown: int = 0
    considered: int = 0  # entity-total, non-nil facts
    dimensioned: int = 0
    nil: int = 0
    mapped_by_method: Counter[str] = field(default_factory=Counter)
    mapped_by_statement: Counter[str] = field(default_factory=Counter)
    known_by_reason: Counter[str] = field(default_factory=Counter)
    not_applicable: int = 0
    placeholder_zero: int = 0  # reported 0 for a concept where 0 means "not disclosed"
    unmapped_count: int = 0

    def stats(self) -> dict[str, Any]:
        """Counts for the jobs row and the CLI."""
        return {
            "candidates": len(self.candidates),
            "considered": self.considered,
            "dimensioned_skipped": self.dimensioned,
            "nil_skipped": self.nil,
            "mapped_by_method": dict(self.mapped_by_method),
            "mapped_by_statement": dict(self.mapped_by_statement),
            "known_unmapped_by_reason": dict(self.known_by_reason),
            "not_applicable": self.not_applicable,
            "placeholder_zero": self.placeholder_zero,
            "unmapped": self.unmapped_count,
            "basis_unknown": self.basis_unknown,
            "issues": dict(Counter(i.reason for i in self.issues)),
        }


def row_to_fact(row: sa.Row) -> FactRow:
    """parsed_facts row -> FactRow (value as Decimal, dates parsed)."""
    return FactRow(
        row.id,
        ParsedFact(
            value=None if row.value is None else Decimal(row.value),
            concept_raw=row.concept_raw,
            label_raw=row.label_raw,
            period_start=date.fromisoformat(row.period_start) if row.period_start else None,
            period_end=date.fromisoformat(row.period_end) if row.period_end else None,
            basis=row.basis or Basis.UNKNOWN.value,
            unit_raw=row.unit_raw,
            scale=row.scale,
            decimals=row.decimals,
            source_loc=row.source_loc,
            extractor=row.extractor,
            ocr_backend=row.ocr_backend,
            confidence=row.confidence if row.confidence is not None else 1.0,
            statement_type=row.statement_type,
        ),
        row.dimensions_json is not None,
    )


def _example(fact: ParsedFact) -> dict[str, Any]:
    """A fact -> a small payload showing what was seen (value, period, source)."""
    return {
        "value": None if fact.value is None else str(fact.value),
        "period_start": fact.period_start.isoformat() if fact.period_start else None,
        "period_end": fact.period_end.isoformat() if fact.period_end else None,
        "basis": fact.basis,
        "extractor": fact.extractor,
    }


def _fact_key(row: FactRow) -> str:
    """A short description of a fact for issue keys."""
    f = row.fact
    return f"{f.concept_raw or f.label_raw}|{f.period_start}|{f.period_end}|{f.basis}"


def _cash_flow_starts(
    mapped: list[tuple[FactRow, Mapped]], taxonomy: Taxonomy, filing_basis: str | None
) -> dict[str, date]:
    """Earliest start of the cash-flow durations per basis (to tell opening from closing cash)."""
    starts: dict[str, date] = {}
    for row, outcome in mapped:
        concept = taxonomy.concepts[outcome.concept]
        start = row.fact.period_start
        if concept.statement != "cash_flow" or concept.period_kind != "duration" or start is None:
            continue
        basis = resolve_basis(row.fact.basis, filing_basis).value
        if basis not in starts or start < starts[basis]:
            starts[basis] = start
    return starts


def _concept_for(
    row: FactRow,
    outcome: Mapped,
    concept: Concept,
    starts: dict[str, date],
    filing_basis: str | None,
) -> str:
    """Closing cash dated before the cash-flow period starts is the opening balance."""
    end = row.fact.period_end
    if concept.opening_concept is None or end is None:
        return outcome.concept
    start = starts.get(resolve_basis(row.fact.basis, filing_basis).value)
    return concept.opening_concept if start is not None and end < start else outcome.concept


def _normalize_mapped(
    row: FactRow,
    name: str,
    method: str,
    ctx: FilingContext,
    taxonomy: Taxonomy,
    out: NormalizeOutcome,
    min_confidence: float,
    covered: frozenset[tuple[str, date, date, str]],
) -> CanonicalCandidate | None:
    """A mapped fact -> candidate (unit, period, basis), or None with the reason queued."""
    fact, concept = row.fact, taxonomy.concepts[name]

    def reject(reason: str, detail: str) -> None:
        out.issues.append(IssueRequest(reason, _fact_key(row), {"concept": name, "detail": detail}))

    converted = to_canonical(fact, concept, taxonomy)
    if converted.value is None:
        reject(converted.reason or "unit", converted.detail)
        return None
    anchor = None
    if concept.statement == "cash_flow" and concept.period_kind == "instant":
        anchor = ANCHOR_DAY_BEFORE_START if name in taxonomy.opening_names else ANCHOR_END
    if concept.zero_is_placeholder and converted.value == 0:
        out.placeholder_zero += 1  # never store a false 0
        return None
    placed = classify(fact.period_start, fact.period_end, concept.period_kind, anchor)
    if placed.period is None:
        reject(placed.reason or "period", placed.detail)
        return None
    basis = resolve_basis(fact.basis, ctx.basis)
    if basis is Basis.UNKNOWN:
        out.basis_unknown += 1
        return None
    period = placed.period
    candidate = CanonicalCandidate(
        isin=ctx.isin,
        filing_id=ctx.filing_id,
        parsed_fact_id=row.id,
        concept=name,
        period_start=period.start,
        period_end=period.end,
        period_type=period.type.value,
        basis=basis.value,
        value=converted.value,
        unit=concept.unit,
        mapping_method=method,
        extractor=fact.extractor,
        ocr_backend=fact.ocr_backend,
        confidence=fact.confidence,
        filed_on=ctx.filed_on,
    )
    if (
        fact.extractor != EXTRACTOR_XBRL
        and fact.confidence < min_confidence
        and candidate.key not in covered
    ):
        out.issues.append(
            IssueRequest(
                ISSUE_LOW_CONFIDENCE,
                _fact_key(row),
                {
                    "concept": name,
                    "value": str(converted.value),
                    "confidence": fact.confidence,
                    "detail": "no XBRL fact covers it; this is the only source",
                },
            )
        )
    return candidate


def _drop_conflicting(out: NormalizeOutcome) -> None:
    """Two candidates with one key: equal values keep the first; different values queue an issue."""
    first: dict[tuple[str, date, date, str], CanonicalCandidate] = {}
    conflicting: set[tuple[str, date, date, str]] = set()
    for cand in out.candidates:
        seen = first.setdefault(cand.key, cand)
        if seen is not cand and seen.value != cand.value:
            conflicting.add(cand.key)
    for key in conflicting:
        values = sorted({str(c.value) for c in out.candidates if c.key == key})
        out.issues.append(
            IssueRequest(
                ISSUE_DUPLICATE_CANDIDATES,
                f"{key[0]}|{key[1]}|{key[2]}|{key[3]}",
                {"concept": key[0], "values": values},
            )
        )
    out.candidates = [c for k, c in first.items() if k not in conflicting]


def _check_duplicates(
    duplicates: list[tuple[FactRow, str, str]],
    ctx: FilingContext,
    taxonomy: Taxonomy,
    out: NormalizeOutcome,
) -> None:
    """A known duplicate QName must equal its target concept's value for the same period."""
    by_key = {c.key: c for c in out.candidates}
    for row, qname, target in duplicates:
        concept = taxonomy.concepts[target]
        converted = to_canonical(row.fact, concept, taxonomy)
        placed = classify(row.fact.period_start, row.fact.period_end, concept.period_kind)
        basis = resolve_basis(row.fact.basis, ctx.basis)
        if converted.value is None or placed.period is None or basis is Basis.UNKNOWN:
            continue
        other = by_key.get((target, placed.period.start, placed.period.end, basis.value))
        if other is not None and other.value != converted.value:
            out.issues.append(
                IssueRequest(
                    ISSUE_DUPLICATE_MISMATCH,
                    _fact_key(row),
                    {
                        "concept": target,
                        "duplicate_qname": qname,
                        "duplicate_value": str(converted.value),
                        "mapped_value": str(other.value),
                    },
                )
            )


def normalize_facts(
    rows: Sequence[FactRow],
    ctx: FilingContext,
    mapper: Mapper,
    min_confidence: float,
    covered: frozenset[tuple[str, date, date, str]] = frozenset(),
) -> NormalizeOutcome:
    """A filing's parsed facts -> candidates, counts and review requests (pure; no database)."""
    taxonomy = mapper.taxonomy
    out = NormalizeOutcome()
    mapped: list[tuple[FactRow, Mapped]] = []
    duplicates: list[tuple[FactRow, str, str]] = []
    for row in rows:
        if row.dimensioned:
            out.dimensioned += 1
            continue
        if row.fact.value is None:
            out.nil += 1
            continue
        out.considered += 1
        if not (row.fact.concept_raw or row.fact.label_raw):
            out.issues.append(IssueRequest(ISSUE_NO_NAME, _fact_key(row), {"fact_id": row.id}))
            continue
        result = mapper.map_fact(row.fact, ctx.family, ctx.sector)
        if isinstance(result, Mapped):
            mapped.append((row, result))
        elif isinstance(result, NotApplicable):
            out.not_applicable += 1
        elif isinstance(result, Known):
            out.known_by_reason[result.reason] += 1
            entry = result.entry
            if entry is not None and entry.duplicate_of:
                duplicates.append((row, entry.qname, entry.duplicate_of))
        elif isinstance(result, Unmapped):
            out.unmapped_count += 1
            out.unmapped.append(UnmappedRequest(result, _example(row.fact)))
    starts = _cash_flow_starts(mapped, taxonomy, ctx.basis)
    for row, result in mapped:
        concept = taxonomy.concepts[result.concept]
        name = _concept_for(row, result, concept, starts, ctx.basis)
        candidate = _normalize_mapped(
            row, name, result.method, ctx, taxonomy, out, min_confidence, covered
        )
        if candidate is not None:
            out.candidates.append(candidate)
            out.mapped_by_method[result.method] += 1
            out.mapped_by_statement[taxonomy.concepts[name].statement] += 1
    _drop_conflicting(out)
    _check_duplicates(duplicates, ctx, taxonomy, out)
    return out


# --- persistence ------------------------------------------------------------------------------


@dataclass
class NormalizeReport:
    """Outcome for one filing."""

    filing_id: str
    status: str  # normalized | skipped | failed
    stats: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _context(filing: sa.Row, sector: str, taxonomy: Taxonomy) -> FilingContext:
    """Filing row (+ entity sector) -> FilingContext; raises if the taxonomy family is unknown."""
    family = ""
    if filing.format == FilingFormat.XBRL.value:
        family = filing.taxonomy_family or ""
        if family not in taxonomy.families:
            raise NormalizeError(
                f"filing taxonomy family '{family}' has no mappings in the taxonomy registry "
                f"(known: {', '.join(taxonomy.families)})"
            )
    return FilingContext(
        filing.filing_id, filing.isin, family, sector, filing.basis, filing.filed_on
    )


def load_rows(filing_id: str, engine: Engine | None = None) -> list[FactRow]:
    """filing_id -> its parsed_facts as FactRows (in id order)."""
    facts = tables.parsed_facts
    with transaction(engine) as conn:
        found = conn.execute(
            sa.select(facts).where(facts.c.filing_id == filing_id).order_by(facts.c.id)
        ).all()
    return [row_to_fact(r) for r in found]


def entity_sector(isin: str, engine: Engine | None = None) -> str:
    """ISIN -> the entity's sector_type."""
    with transaction(engine) as conn:
        value = conn.execute(
            sa.select(tables.entities.c.sector_type).where(tables.entities.c.isin == isin)
        ).scalar()
    if value is None:
        raise NormalizeError(f"unknown entity {isin}")
    return str(value)


def xbrl_keys(
    isin: str, mapper: Mapper, min_confidence: float, engine: Engine | None = None
) -> frozenset[tuple[str, date, date, str]]:
    """Keys (concept, start, end, basis) that the ISIN's parsed XBRL filings cover."""
    filings = tables.filings
    with transaction(engine) as conn:
        found = conn.execute(
            sa.select(filings).where(
                filings.c.isin == isin,
                filings.c.format == FilingFormat.XBRL.value,
                filings.c.status.in_([FilingStatus.PARSED.value, FilingStatus.NORMALIZED.value]),
            )
        ).all()
    sector = entity_sector(isin, engine)
    keys: set[tuple[str, date, date, str]] = set()
    for filing in found:
        ctx = _context(filing, sector, mapper.taxonomy)
        outcome = normalize_facts(load_rows(filing.filing_id, engine), ctx, mapper, min_confidence)
        keys.update(c.key for c in outcome.candidates)
    return frozenset(keys)


def _persist_requests(ctx: FilingContext, out: NormalizeOutcome, engine: Engine | None) -> None:
    """Queue the unmapped items, basis item and issues of one filing (one transaction)."""
    with transaction(engine) as conn:
        for req in out.unmapped:
            queue.queue_unmapped(
                conn,
                ctx.isin,
                ctx.family,
                req.outcome.key,
                req.outcome.key_type,
                ctx.filing_id,
                req.outcome.suggestion,
                req.example,
                req.outcome.statement,
            )
        if out.basis_unknown:
            queue.queue_basis(conn, ctx.isin, ctx.filing_id, out.basis_unknown)
        for issue in out.issues:
            queue.queue_issue(conn, ctx.isin, ctx.filing_id, issue.reason, issue.key, issue.detail)


def normalize_filing(
    filing: sa.Row,
    mapper: Mapper,
    writer: CandidateWriter,
    settings: Settings,
    covered_for: Callable[[str], frozenset[tuple[str, date, date, str]]],
    engine: Engine | None = None,
) -> NormalizeReport:
    """One `parsed` filing -> review items, candidates to the writer, status `normalized`."""
    report = NormalizeReport(filing.filing_id, FilingStatus.NORMALIZED.value)
    job_id = start_job(JOB_TYPE, filing.isin, engine)
    try:
        with bind(isin=filing.isin, filing_id=filing.filing_id):
            ctx = _context(filing, entity_sector(filing.isin, engine), mapper.taxonomy)
            covered = (
                frozenset()
                if filing.format == FilingFormat.XBRL.value
                else covered_for(filing.isin)
            )
            out = normalize_facts(
                load_rows(filing.filing_id, engine),
                ctx,
                mapper,
                settings.parsing.pdf.min_table_confidence,
                covered,
            )
            report.stats = out.stats()
            _persist_requests(ctx, out, engine)
            writer(out.candidates)
        registry.set_status(filing.filing_id, FilingStatus.NORMALIZED, None, engine)
        finish_job(job_id, JobStatus.SUCCEEDED, report.stats, None, engine)
    except NormalizeError as exc:
        report.status = "skipped"
        report.error = str(exc)
        finish_job(job_id, JobStatus.FAILED, report.stats, report.error, engine)
    except Exception as exc:  # one bad filing must not stop the run; it stays `parsed`
        report.status = "failed"
        report.error = f"{type(exc).__name__}: {exc}"
        finish_job(job_id, JobStatus.FAILED, report.stats, report.error, engine)
        log.error("normalize failed: %s", report.error, extra={"event": "normalize_failed"})
    return report


def _select(filing_id: str | None, engine: Engine | None) -> list[sa.Row]:
    """Filings to normalize: all `parsed` ones, or the one named (which must be `parsed`)."""
    if filing_id is None:
        return registry.pending(FilingStatus.PARSED, engine)
    with transaction(engine) as conn:
        row = conn.execute(
            sa.select(tables.filings).where(tables.filings.c.filing_id == filing_id)
        ).first()
    if row is None:
        raise NormalizeError(f"unknown filing: {filing_id}")
    if row.status != FilingStatus.PARSED.value:
        raise NormalizeError(f"{filing_id} is {row.status}; only parsed filings can be normalized")
    return [row]


def run_normalize(
    writer: CandidateWriter,
    filing_id: str | None = None,
    engine: Engine | None = None,
    settings: Settings | None = None,
    taxonomy: Taxonomy | None = None,
) -> list[NormalizeReport]:
    """Normalize every `parsed` filing (or the one named), passing candidates to `writer`."""
    settings = settings or get_settings()
    filings = _select(filing_id, engine)
    if not filings:
        return []
    mapper = Mapper(taxonomy or load_taxonomy(), queue.load_resolutions(engine))
    cache: dict[str, frozenset[tuple[str, date, date, str]]] = {}

    def covered_for(isin: str) -> frozenset[tuple[str, date, date, str]]:
        """XBRL keys of an ISIN, computed once per run."""
        if isin not in cache:
            cache[isin] = xbrl_keys(isin, mapper, settings.parsing.pdf.min_table_confidence, engine)
        return cache[isin]

    return [normalize_filing(f, mapper, writer, settings, covered_for, engine) for f in filings]
