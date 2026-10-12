"""Register filings exactly once, keep the review queue for ingestion, and look up entities."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from finrag.core.db import tables, transaction
from finrag.core.types import FilingStatus, looks_like_isin, utc_now_iso
from finrag.ingestion.models import Conflict, FilingCandidate

ITEM_CONFLICT = "filing_conflict"
ITEM_UNRECOGNISED = "inbox_unrecognised"
ITEM_LINK_ONLY = "transcript_link_only"
REVISION = "revision"
ORIGINAL = "original"


class RegistryError(Exception):
    """Raised for an invalid registry operation; the message says why."""


@dataclass(frozen=True)
class RawFile:
    """Where a raw file landed."""

    filing_id: str
    path: Path
    sha256: str


class DbEntityLookup:
    """Identifier <-> ISIN lookups against the entities and entity_aliases tables."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    def isin_for(self, identifier: str) -> str | None:
        """ISIN | NSE symbol | BSE code | alias -> the entity's ISIN, or None if unknown."""
        text = identifier.strip()
        ents, aliases = tables.entities, tables.entity_aliases
        with transaction(self._engine) as conn:
            if looks_like_isin(text):
                row = conn.execute(
                    sa.select(ents.c.isin).where(ents.c.isin == text.upper())
                ).first()
                if row:
                    return row.isin
            row = conn.execute(
                sa.select(ents.c.isin).where(
                    sa.or_(
                        sa.func.upper(ents.c.nse_symbol) == text.upper(), ents.c.bse_code == text
                    )
                )
            ).first()
            if row:
                return row.isin
            row = conn.execute(
                sa.select(aliases.c.isin).where(sa.func.upper(aliases.c.alias) == text.upper())
            ).first()
            return row.isin if row else None

    def symbol_for(self, isin: str) -> str | None:
        """ISIN -> NSE symbol, or None."""
        with transaction(self._engine) as conn:
            return conn.execute(
                sa.select(tables.entities.c.nse_symbol).where(tables.entities.c.isin == isin)
            ).scalar()

    def tracked_isins(self) -> list[str]:
        """ISINs of active watch-list entities."""
        with transaction(self._engine) as conn:
            return list(
                conn.execute(
                    sa.select(tables.entities.c.isin)
                    .where(tables.entities.c.status == "active")
                    .order_by(tables.entities.c.isin)
                ).scalars()
            )


# --- review queue -----------------------------------------------------------------------------


def queue_review(
    conn: Connection, item_type: str, payload: dict[str, Any], match: dict[str, Any]
) -> int:
    """Create an open item, or refresh the open one of this type whose payload matches `match`."""
    queue = tables.review_queue
    now = utc_now_iso()
    text = json.dumps(payload, default=str, sort_keys=True)
    open_items = conn.execute(
        sa.select(queue).where(queue.c.item_type == item_type, queue.c.status == "open")
    ).all()
    for item in open_items:
        existing = json.loads(item.payload_json)
        if all(existing.get(key) == value for key, value in match.items()):
            conn.execute(
                sa.update(queue)
                .where(queue.c.id == item.id)
                .values(payload_json=text, occurrences=item.occurrences + 1, updated_at=now)
            )
            return item.id
    result = conn.execute(
        sa.insert(queue).values(
            item_type=item_type,
            payload_json=text,
            occurrences=1,
            status="open",
            created_at=now,
            updated_at=now,
        )
    )
    return int(result.inserted_primary_key[0])


def queue_unrecognised(file_name: str, reason: str, engine: Engine | None = None) -> None:
    """A source file that could not be interpreted -> inbox_unrecognised item."""
    with transaction(engine) as conn:
        queue_review(
            conn,
            ITEM_UNRECOGNISED,
            {"file_name": file_name, "reason": reason},
            {"file_name": file_name},
        )


def queue_link_only(payload: dict[str, Any], engine: Engine | None = None) -> None:
    """A link-only transcript cover letter -> transcript_link_only item (one per file hash)."""
    with transaction(engine) as conn:
        queue_review(conn, ITEM_LINK_ONLY, payload, {"sha256": payload["sha256"]})


# --- filings ----------------------------------------------------------------------------------


def filing_by_sha(conn: Connection, sha256: str) -> sa.Row | None:
    """sha256 -> the filings row, if registered."""
    return conn.execute(sa.select(tables.filings).where(tables.filings.c.sha256 == sha256)).first()


def known_url(url: str, engine: Engine | None = None) -> bool:
    """True if a filing with this source URL is already registered (skip the download)."""
    with transaction(engine) as conn:
        return (
            conn.execute(
                sa.select(tables.filings.c.filing_id).where(tables.filings.c.source_url == url)
            ).first()
            is not None
        )


def _next_revision_no(conn: Connection, candidate: FilingCandidate, isin: str) -> int:
    """1 + the highest revision_no of the same filing (0 when only the original exists)."""
    f = tables.filings
    rows = conn.execute(
        sa.select(f.c.revision_no).where(
            f.c.isin == isin,
            f.c.filing_type == candidate.filing_type,
            f.c.period_end == (candidate.period_end.isoformat() if candidate.period_end else None),
            f.c.basis == candidate.basis.value,
        )
    ).scalars()
    return max((r or 0 for r in rows), default=0) + 1


def superseding_revisions(conn: Connection, filing: sa.Row) -> list[str]:
    """Filing -> IDs of non-quarantined revisions of the same ISIN, type, period and basis."""
    f = tables.filings
    rows = conn.execute(
        sa.select(f.c.filing_id).where(
            f.c.isin == filing.isin,
            f.c.filing_type == filing.filing_type,
            f.c.period_end == filing.period_end,
            f.c.basis == filing.basis,
            f.c.submission == REVISION,
            f.c.status != FilingStatus.QUARANTINED.value,
            f.c.filing_id != filing.filing_id,
        )
    ).scalars()
    return sorted(rows)


def _link_superseded(conn: Connection, new_filing: sa.Row) -> None:
    """A revision arrived: name it in open conflict items of the filings it supersedes."""
    if new_filing.submission != REVISION or new_filing.status == FilingStatus.QUARANTINED.value:
        return
    queue = tables.review_queue
    key = (new_filing.isin, new_filing.filing_type, new_filing.period_end, new_filing.basis)
    for item in conn.execute(
        sa.select(queue).where(queue.c.item_type == ITEM_CONFLICT, queue.c.status == "open")
    ).all():
        payload = json.loads(item.payload_json)
        other = conn.execute(
            sa.select(tables.filings).where(tables.filings.c.filing_id == payload["filing_id"])
        ).first()
        if other is None or new_filing.filing_id in payload["superseded_by"]:
            continue
        if (other.isin, other.filing_type, other.period_end, other.basis) == key:
            payload["superseded_by"] = sorted([*payload["superseded_by"], new_filing.filing_id])
            conn.execute(
                sa.update(queue)
                .where(queue.c.id == item.id)
                .values(payload_json=json.dumps(payload, sort_keys=True), updated_at=utc_now_iso())
            )


def register(
    candidate: FilingCandidate,
    raw: RawFile,
    conflicts: Sequence[Conflict] = (),
    taxonomy_family: str | None = None,
    engine: Engine | None = None,
) -> tuple[str, bool]:
    """Candidate + stored raw file -> (filing_id, is_new); a known sha256 is a duplicate."""
    if candidate.isin is None:
        raise RegistryError("cannot register a filing without an ISIN")
    with transaction(engine) as conn:
        existing = filing_by_sha(conn, raw.sha256)
        if existing:
            return existing.filing_id, False
        revision_no = candidate.revision_no
        if revision_no is None:
            revision_no = (
                _next_revision_no(conn, candidate, candidate.isin)
                if candidate.submission == REVISION
                else 0
            )
        status = FilingStatus.QUARANTINED if conflicts else FilingStatus.FETCHED
        conn.execute(
            sa.insert(tables.filings).values(
                filing_id=raw.filing_id,
                isin=candidate.isin,
                source=candidate.source,
                filing_type=candidate.filing_type,
                submission=candidate.submission,
                revision_no=revision_no,
                taxonomy_family=taxonomy_family,
                basis=candidate.basis.value,
                period_start=candidate.period_start.isoformat() if candidate.period_start else None,
                period_end=candidate.period_end.isoformat() if candidate.period_end else None,
                period_type=candidate.period_type.value if candidate.period_type else None,
                filed_on=candidate.filed_on,
                source_url=candidate.source_url,
                raw_path=str(raw.path),
                sha256=raw.sha256,
                format=candidate.format.value,
                status=status.value,
                error=None,
                created_at=utc_now_iso(),
            )
        )
        row = conn.execute(
            sa.select(tables.filings).where(tables.filings.c.filing_id == raw.filing_id)
        ).one()
        if conflicts:
            payload = {
                "filing_id": raw.filing_id,
                "file_name": candidate.local_path.name if candidate.local_path else raw.path.name,
                "conflicts": [c.as_dict() for c in conflicts],
                "superseded_by": superseding_revisions(conn, row),
            }
            queue_review(conn, ITEM_CONFLICT, payload, {"filing_id": raw.filing_id})
        _link_superseded(conn, row)
        return raw.filing_id, True


def pending(status: FilingStatus | str, engine: Engine | None = None) -> list[sa.Row]:
    """Status -> filings in that status, oldest first."""
    value = FilingStatus(status).value
    with transaction(engine) as conn:
        return list(
            conn.execute(
                sa.select(tables.filings)
                .where(tables.filings.c.status == value)
                .order_by(tables.filings.c.created_at, tables.filings.c.filing_id)
            )
        )


def set_status(
    filing_id: str, status: FilingStatus, error: str | None = None, engine: Engine | None = None
) -> None:
    """Move a filing along fetched -> parsed -> normalized, or to failed with an error."""
    with transaction(engine) as conn:
        result = conn.execute(
            sa.update(tables.filings)
            .where(tables.filings.c.filing_id == filing_id)
            .values(status=FilingStatus(status).value, error=error)
        )
        if result.rowcount == 0:
            raise RegistryError(f"unknown filing: {filing_id}")


def _resolve_conflict(
    filing_id: str,
    item_status: str,
    filing_status: FilingStatus | None,
    note: str,
    engine: Engine | None,
) -> None:
    """Close the open filing_conflict item for a quarantined filing and optionally move it."""
    queue = tables.review_queue
    with transaction(engine) as conn:
        filing = conn.execute(
            sa.select(tables.filings).where(tables.filings.c.filing_id == filing_id)
        ).first()
        if filing is None:
            raise RegistryError(f"unknown filing: {filing_id}")
        if filing.status != FilingStatus.QUARANTINED.value:
            raise RegistryError(f"{filing_id} is {filing.status}, not quarantined")
        for item in conn.execute(
            sa.select(queue).where(queue.c.item_type == ITEM_CONFLICT, queue.c.status == "open")
        ).all():
            if json.loads(item.payload_json).get("filing_id") == filing_id:
                conn.execute(
                    sa.update(queue)
                    .where(queue.c.id == item.id)
                    .values(
                        status=item_status,
                        resolution_json=json.dumps({"action": item_status, "note": note}),
                        updated_at=utc_now_iso(),
                    )
                )
        if filing_status is not None:
            conn.execute(
                sa.update(tables.filings)
                .where(tables.filings.c.filing_id == filing_id)
                .values(status=filing_status.value)
            )


def release(filing_id: str, note: str = "", engine: Engine | None = None) -> None:
    """Quarantined filing -> fetched (later modules may use it); its review item is resolved."""
    _resolve_conflict(filing_id, "resolved", FilingStatus.FETCHED, note, engine)


def ignore(filing_id: str, note: str = "", engine: Engine | None = None) -> None:
    """Keep a filing quarantined for good; its review item is marked ignored."""
    _resolve_conflict(filing_id, "ignored", None, note, engine)
