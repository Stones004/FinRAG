"""Append-only writer for canonical_facts. Nothing here updates or deletes a fact."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from finrag.core.config import Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.ids import new_id
from finrag.core.logging import get_logger
from finrag.core.types import FilingStatus, Period, PeriodType, SourceClass, utc_now_iso
from finrag.facts import restatement
from finrag.facts.current_view import load_facts
from finrag.facts.models import Fact
from finrag.normalize.models import CanonicalCandidate
from finrag.parsing.models import EXTRACTOR_OCR, EXTRACTOR_TABLE, EXTRACTOR_XBRL

PREFIX_FACT = "FCT"

log = get_logger("facts")

_CLASS_BY_EXTRACTOR = {
    EXTRACTOR_XBRL: SourceClass.XBRL,
    EXTRACTOR_TABLE: SourceClass.PDF,
    EXTRACTOR_OCR: SourceClass.OCR,
}


class WriterError(Exception):
    """Raised when facts cannot be written; the message says why."""


@dataclass
class WriteStats:
    """What a write did: counts, the new fact IDs and the ISINs touched."""

    inserted: int = 0
    skipped: int = 0
    restated: int = 0  # restatement rows recorded
    flagged: int = 0  # of those, flagged
    refused: int = 0  # candidates of a quarantined filing
    fact_ids: list[str] = field(default_factory=list)
    restatement_ids: list[int] = field(default_factory=list)
    isins: set[str] = field(default_factory=set)

    def merge(self, other: WriteStats) -> None:
        """Add another run's counts and IDs to this one."""
        self.inserted += other.inserted
        self.skipped += other.skipped
        self.restated += other.restated
        self.flagged += other.flagged
        self.refused += other.refused
        self.fact_ids += other.fact_ids
        self.restatement_ids += other.restatement_ids
        self.isins |= other.isins

    def as_dict(self) -> dict[str, int]:
        """Counts only, for the CLI and jobs rows."""
        return {
            "inserted": self.inserted,
            "skipped": self.skipped,
            "restated": self.restated,
            "flagged": self.flagged,
            "refused": self.refused,
        }


@dataclass(frozen=True)
class NewFact:
    """A fact to append (candidate or derived), before it has an ID."""

    isin: str
    concept: str
    period: Period
    basis: str
    value: Decimal
    unit: str
    source_filing_id: str
    parsed_fact_id: int | None
    mapping_method: str | None
    source_class: SourceClass
    filed_on: str | None
    derivation: dict[str, Any] | None = None


def _same_input(existing: Fact, new: NewFact) -> bool:
    """True when `existing` is the same fact as `new` (idempotent re-run)."""
    if new.derivation is not None:
        old = existing.derivation or {}
        return existing.is_derived and set(old.get("input_fact_ids", [])) == set(
            new.derivation["input_fact_ids"]
        )
    return (
        not existing.is_derived
        and existing.source_filing_id == new.source_filing_id
        and existing.value == new.value
    )


def _record_neighbours(
    conn: Connection, stored: Fact, versions: Sequence[Fact], settings: Settings, stats: WriteStats
) -> None:
    """Judge the new fact against its neighbours in current-view order (same chain only)."""
    chain = sorted((f for f in versions if f.chain == stored.chain), key=lambda f: f.order)
    at = next(i for i, f in enumerate(chain) if f.fact_id == stored.fact_id)
    pairs: list[tuple[Fact, Fact]] = []
    if at > 0:
        pairs.append((chain[at - 1], stored))
    if at + 1 < len(chain):
        pairs.append((stored, chain[at + 1]))
    for old, new in pairs:
        if old.value == new.value:
            continue
        row_id = restatement.record(conn, old, new, settings)
        if row_id is not None:
            stats.restated += 1
            stats.restatement_ids.append(row_id)
            stats.flagged += int(restatement.assess(old, new, settings).flagged)


def append_fact(conn: Connection, new: NewFact, settings: Settings, stats: WriteStats) -> None:
    """Append one fact unless it is a repeat; record restatements against its neighbours."""
    period = Period(new.period.start, new.period.end, new.period.type)
    existing = load_facts(conn, new.isin, [new.concept], new.basis, [period])
    if any(_same_input(f, new) for f in existing):
        stats.skipped += 1
        return
    fact_id = new_id(PREFIX_FACT)
    conn.execute(
        sa.insert(tables.canonical_facts).values(
            fact_id=fact_id,
            isin=new.isin,
            concept=new.concept,
            period_start=new.period.start.isoformat(),
            period_end=new.period.end.isoformat(),
            period_type=new.period.type.value,
            basis=new.basis,
            value=str(new.value),
            unit=new.unit,
            source_filing_id=new.source_filing_id,
            parsed_fact_id=new.parsed_fact_id,
            mapping_method=new.mapping_method,
            is_derived=int(new.derivation is not None),
            derivation_json=(
                None if new.derivation is None else json.dumps(new.derivation, sort_keys=True)
            ),
            filed_on=new.filed_on,
            created_at=utc_now_iso(),
            source_class=new.source_class.value,
        )
    )
    stats.inserted += 1
    stats.fact_ids.append(fact_id)
    stats.isins.add(new.isin)
    versions = load_facts(conn, new.isin, [new.concept], new.basis, [period])
    stored = next(f for f in versions if f.fact_id == fact_id)
    _record_neighbours(conn, stored, versions, settings, stats)


def _filing_status(conn: Connection, filing_id: str) -> tuple[str, int]:
    """filing_id -> (status, revision_no); raises WriterError for an unknown filing."""
    row = conn.execute(
        sa.select(tables.filings.c.status, tables.filings.c.revision_no).where(
            tables.filings.c.filing_id == filing_id
        )
    ).first()
    if row is None:
        raise WriterError(f"candidate refers to an unknown filing: {filing_id}")
    return row.status, row.revision_no or 0


def write_candidates(
    candidates: Sequence[CanonicalCandidate],
    engine: Engine | None = None,
    settings: Settings | None = None,
) -> WriteStats:
    """M04 candidates -> appended facts (one transaction); repeats skipped, changes restated."""
    settings = settings or get_settings()
    stats = WriteStats()
    seen: dict[str, tuple[str, int]] = {}
    with transaction(engine) as conn:
        for cand in candidates:
            if cand.filing_id not in seen:
                seen[cand.filing_id] = _filing_status(conn, cand.filing_id)
            status, revision_no = seen[cand.filing_id]
            if status == FilingStatus.QUARANTINED.value:
                stats.refused += 1
                continue
            if cand.revision_no != revision_no:
                log.warning(
                    "candidate revision_no differs from its filing",
                    extra={"event": "revision_mismatch", "fields": {"filing_id": cand.filing_id}},
                )
            append_fact(
                conn,
                NewFact(
                    isin=cand.isin,
                    concept=cand.concept,
                    period=Period(cand.period_start, cand.period_end, PeriodType(cand.period_type)),
                    basis=cand.basis,
                    value=cand.value,
                    unit=cand.unit,
                    source_filing_id=cand.filing_id,
                    parsed_fact_id=cand.parsed_fact_id,
                    mapping_method=cand.mapping_method,
                    source_class=_CLASS_BY_EXTRACTOR[cand.extractor],
                    filed_on=cand.filed_on,
                ),
                settings,
                stats,
            )
    return stats
