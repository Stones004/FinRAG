"""Per-company data status for the UI: latest period, latest transcript, open review items."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.db import tables, transaction
from finrag.core.types import FilingStatus
from finrag.normalize.review_queue import STATUS_OPEN
from finrag.parsing.pdf_text import FILING_TYPE_TRANSCRIPT


@dataclass(frozen=True)
class CompanyStatus:
    """One watched company: how far its data reaches."""

    isin: str
    name: str
    status: str
    latest_period_end: str | None  # latest period end among the stored facts
    latest_transcript_period_end: (
        str | None
    )  # latest quarter with a usable earnings call transcript


def company_status(engine: Engine | None = None) -> list[CompanyStatus]:
    """One row per entity, by name."""
    facts, filings, entities = tables.canonical_facts, tables.filings, tables.entities
    with transaction(engine) as conn:
        latest_facts = conn.execute(
            sa.select(facts.c.isin, sa.func.max(facts.c.period_end)).group_by(facts.c.isin)
        )
        latest = {isin: end for isin, end in latest_facts}
        usable_transcripts = conn.execute(
            sa.select(filings.c.isin, sa.func.max(filings.c.period_end))
            .where(
                filings.c.filing_type == FILING_TYPE_TRANSCRIPT,
                filings.c.status.notin_(
                    [FilingStatus.FAILED.value, FilingStatus.QUARANTINED.value]
                ),
            )
            .group_by(filings.c.isin)
        )
        transcripts = {isin: end for isin, end in usable_transcripts}
        rows = conn.execute(
            sa.select(entities.c.isin, entities.c.name, entities.c.status).order_by(entities.c.name)
        ).all()
    return [
        CompanyStatus(
            r.isin, r.name or r.isin, r.status or "", latest.get(r.isin), transcripts.get(r.isin)
        )
        for r in rows
    ]


def open_review_counts(engine: Engine | None = None) -> dict[str, int]:
    """Open review-queue items by type (items, not occurrences)."""
    queue = tables.review_queue
    with transaction(engine) as conn:
        rows = conn.execute(
            sa.select(queue.c.item_type, sa.func.count())
            .where(queue.c.status == STATUS_OPEN)
            .group_by(queue.c.item_type)
            .order_by(queue.c.item_type)
        )
        return {item_type: int(n) for item_type, n in rows}
