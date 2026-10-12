"""Sync the watch list file into the entities, entity_aliases and review_queue tables."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from finrag.core.config import Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.logging import get_logger
from finrag.core.types import AliasType, utc_now_iso
from finrag.watchlist.master import SecuritiesMaster, SecurityRecord, load_master
from finrag.watchlist.resolver import Resolution, resolve
from finrag.watchlist.schema import WatchlistCompany, WatchlistFile, load_watchlist

REVIEW_ITEM_TYPE = "entity_ambiguous"
RENAME_SOURCE = "securities_master"

log = get_logger("watchlist")


@dataclass(frozen=True)
class NeedsReview:
    """A watch list entry that could not be turned into an entity."""

    identifier: str
    status: str  # ambiguous | not_found | duplicate
    reason: str


@dataclass
class SyncReport:
    """Result of one sync; ISIN lists except `needs_review`.

    A status change appears both in `updated` and in `paused` / `reactivated`.
    """

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    needs_review: list[NeedsReview] = field(default_factory=list)

    @property
    def newly_added(self) -> list[str]:
        """ISINs added by this sync, for M02 to backfill."""
        return list(self.added)


def today_in_app_tz(settings: Settings | None = None) -> date:
    """Today's date in settings.app.timezone (local date if the tz database is unavailable)."""
    settings = settings or get_settings()
    try:
        return datetime.now(ZoneInfo(settings.app.timezone)).date()
    except ZoneInfoNotFoundError:
        return date.today()


def _entity(conn: Connection, isin: str) -> sa.Row | None:
    return conn.execute(sa.select(tables.entities).where(tables.entities.c.isin == isin)).first()


def _alias_rows(conn: Connection, isin: str) -> list[sa.Row]:
    return list(
        conn.execute(sa.select(tables.entity_aliases).where(tables.entity_aliases.c.isin == isin))
    )


def _sync_alias(
    conn: Connection, isin: str, alias_type: AliasType, value: str | None, today: str
) -> bool:
    """Make `value` the one open alias of this type: close others, add it if missing."""
    if not value:
        return False
    aliases = tables.entity_aliases
    open_rows = conn.execute(
        sa.select(aliases).where(
            aliases.c.isin == isin,
            aliases.c.alias_type == alias_type.value,
            aliases.c.valid_to.is_(None),
        )
    ).all()
    changed = False
    for row in open_rows:
        if row.alias != value:
            conn.execute(sa.update(aliases).where(aliases.c.id == row.id).values(valid_to=today))
            changed = True
    if not any(row.alias == value for row in open_rows):
        conn.execute(
            sa.insert(aliases).values(
                isin=isin, alias=value, alias_type=alias_type.value, valid_from=today, valid_to=None
            )
        )
        changed = True
    return changed


def _sync_user_alias(conn: Connection, isin: str, identifier: str, today: str) -> bool:
    """Record the text the user typed unless it already equals the ISIN or any alias."""
    known = {row.alias.upper() for row in _alias_rows(conn, isin)} | {isin}
    if identifier.upper() in known:
        return False
    conn.execute(
        sa.insert(tables.entity_aliases).values(
            isin=isin, alias=identifier, alias_type=AliasType.USER.value, valid_from=today
        )
    )
    return True


def _record_rename(conn: Connection, existing: sa.Row, record: SecurityRecord, today: str) -> None:
    """Add a corporate_events rename row when the master's symbol or name differs from ours."""
    parts = []
    if existing.nse_symbol and record.nse_symbol and existing.nse_symbol != record.nse_symbol:
        parts.append(f"symbol: {existing.nse_symbol} -> {record.nse_symbol}")
    if existing.name and existing.name != record.name:
        parts.append(f"name: {existing.name} -> {record.name}")
    if parts:
        conn.execute(
            sa.insert(tables.corporate_events).values(
                isin=existing.isin,
                event_type="rename",
                event_date=today,
                details="; ".join(parts),
                source=RENAME_SOURCE,
            )
        )


def _queue_review(conn: Connection, identifier: str, status: str, reason: str, found: list) -> None:
    """Create an open entity_ambiguous item, or bump `occurrences` on the existing open one."""
    payload = json.dumps(
        {
            "identifier": identifier,
            "status": status,
            "reason": reason,
            "candidates": [{"isin": r.isin, "name": r.name, "symbol": r.nse_symbol} for r in found],
        }
    )
    queue = tables.review_queue
    now = utc_now_iso()
    open_items = conn.execute(
        sa.select(queue).where(queue.c.item_type == REVIEW_ITEM_TYPE, queue.c.status == "open")
    ).all()
    for item in open_items:
        if json.loads(item.payload_json).get("identifier") == identifier:
            conn.execute(
                sa.update(queue)
                .where(queue.c.id == item.id)
                .values(payload_json=payload, occurrences=item.occurrences + 1, updated_at=now)
            )
            return
    conn.execute(
        sa.insert(queue).values(
            item_type=REVIEW_ITEM_TYPE,
            payload_json=payload,
            occurrences=1,
            status="open",
            created_at=now,
            updated_at=now,
        )
    )


def _listed_by_alias(conn: Connection, identifier: str) -> str | None:
    """ISIN of an existing entity that already carries this ISIN or alias text, else None."""
    isin = conn.execute(
        sa.select(tables.entities.c.isin).where(tables.entities.c.isin == identifier.upper())
    ).scalar()
    if isin:
        return isin
    return conn.execute(
        sa.select(tables.entity_aliases.c.isin).where(
            sa.func.upper(tables.entity_aliases.c.alias) == identifier.upper()
        )
    ).scalar()


def _insert_entity(
    conn: Connection, record: SecurityRecord, wanted: dict[str, str], today: str
) -> None:
    conn.execute(
        sa.insert(tables.entities).values(
            isin=record.isin,
            name=record.name,
            nse_symbol=record.nse_symbol,
            bse_code=record.bse_code,
            added_on=today,
            updated_on=today,
            **wanted,
        )
    )
    _sync_alias(conn, record.isin, AliasType.SYMBOL, record.nse_symbol, today)
    _sync_alias(conn, record.isin, AliasType.CODE, record.bse_code, today)
    _sync_alias(conn, record.isin, AliasType.NAME, record.name, today)


def _update_entity(
    conn: Connection,
    existing: sa.Row,
    record: SecurityRecord,
    wanted: dict[str, str],
    today: str,
) -> tuple[bool, dict[str, str | None]]:
    """Align an existing entity with the master and watch list -> (changed, values)."""
    values = {
        "name": record.name,
        "nse_symbol": record.nse_symbol or existing.nse_symbol,
        "bse_code": record.bse_code or existing.bse_code,
        **wanted,
    }
    changed = any(getattr(existing, key) != value for key, value in values.items())
    if changed:
        _record_rename(conn, existing, record, today)
    changed |= _sync_alias(conn, record.isin, AliasType.SYMBOL, values["nse_symbol"], today)
    changed |= _sync_alias(conn, record.isin, AliasType.CODE, values["bse_code"], today)
    changed |= _sync_alias(conn, record.isin, AliasType.NAME, record.name, today)
    return changed, values


def _sync_company(
    conn: Connection,
    company: WatchlistCompany,
    resolution: Resolution,
    settings: Settings,
    today: str,
    report: SyncReport,
) -> None:
    """Upsert one resolved company and record what happened in the report."""
    record = resolution.candidates[0]
    wanted = {
        "sector_type": company.sector_type or settings.watchlist.default_sector_type,
        "default_basis": company.basis or settings.defaults.basis,
        "status": company.status,
    }
    existing = _entity(conn, record.isin)
    if existing is None:
        _insert_entity(conn, record, wanted, today)
        _sync_user_alias(conn, record.isin, company.id, today)
        report.added.append(record.isin)
        if company.status == "paused":
            report.paused.append(record.isin)
        return
    changed, values = _update_entity(conn, existing, record, wanted, today)
    changed |= _sync_user_alias(conn, record.isin, company.id, today)
    if changed:
        conn.execute(
            sa.update(tables.entities)
            .where(tables.entities.c.isin == record.isin)
            .values(updated_on=today, **values)
        )
        report.updated.append(record.isin)
    else:
        report.unchanged.append(record.isin)
    if existing.status != company.status:
        (report.paused if company.status == "paused" else report.reactivated).append(record.isin)


def _pause_unlisted(conn: Connection, listed: set[str], today: str, report: SyncReport) -> None:
    """Entities no longer on the watch list are paused, never deleted."""
    ents = tables.entities
    rows = conn.execute(sa.select(ents.c.isin).where(ents.c.status == "active")).scalars().all()
    for isin in rows:
        if isin not in listed:
            conn.execute(
                sa.update(ents).where(ents.c.isin == isin).values(status="paused", updated_on=today)
            )
            report.paused.append(isin)
            if isin not in report.updated:
                report.updated.append(isin)
            extra = {"event": "paused", "fields": {"isin": isin}}
            log.info("paused: not in watchlist", extra=extra)


def sync_watchlist(
    master: SecuritiesMaster | None = None,
    engine: Engine | None = None,
    watchlist: WatchlistFile | None = None,
    today: date | None = None,
) -> SyncReport:
    """Resolve every watch list entry and upsert entities/aliases -> SyncReport."""
    settings = get_settings()
    master = master or load_master(settings)
    watchlist = watchlist or load_watchlist()
    day = (today or today_in_app_tz(settings)).isoformat()
    report = SyncReport()
    listed: set[str] = set()
    with transaction(engine) as conn:
        for company in watchlist.companies:
            resolution = resolve(company.id, master, settings)
            if resolution.status != "resolved":
                _queue_review(
                    conn,
                    company.id,
                    resolution.status,
                    resolution.reason,
                    list(resolution.candidates),
                )
                report.needs_review.append(
                    NeedsReview(company.id, resolution.status, resolution.reason)
                )
                known = _listed_by_alias(conn, company.id)
                if known:  # unresolved today, but already tracked: do not pause it
                    listed.add(known)
                continue
            if resolution.isin in listed:
                reason = f"resolves to {resolution.isin}, already listed earlier in the watch list"
                report.needs_review.append(NeedsReview(company.id, "duplicate", reason))
                continue
            listed.add(resolution.isin)
            _sync_company(conn, company, resolution, settings, day, report)
        _pause_unlisted(conn, listed, day, report)
    return report
