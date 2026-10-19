"""Review-queue helpers for normalization: queue items, read user resolutions, resolve/ignore."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from finrag.core.db import tables, transaction
from finrag.core.types import utc_now_iso
from finrag.ingestion.registry import queue_review
from finrag.normalize.mapper import KEY_LABEL, KEY_QNAME, Resolutions
from finrag.normalize.taxonomy import Taxonomy

ITEM_UNMAPPED = "unmapped_concept"
ITEM_AMBIGUOUS_BASIS = "ambiguous_basis"
ITEM_ISSUE = "normalization_issue"

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_IGNORED = "ignored"


class ReviewError(Exception):
    """Raised when a review command cannot be carried out; the message says why."""


def queue_unmapped(
    conn: Connection,
    isin: str,
    family: str,
    key: str,
    key_type: str,
    filing_id: str,
    suggestion: dict[str, object] | None,
    example: dict[str, Any],
    statement: str | None = None,
) -> int:
    """Unmapped QName or label -> one open item per (isin, key); repeats raise `occurrences`."""
    payload = {
        "isin": isin,
        "family": family,
        "key": key,
        "key_type": key_type,
        "filing_id": filing_id,
        "suggestion": suggestion,
        "example": example,
        "statement": statement,
    }
    match = {"isin": isin, "key": key, "key_type": key_type, "statement": statement}
    return queue_review(conn, ITEM_UNMAPPED, payload, match)


def queue_basis(conn: Connection, isin: str, filing_id: str, facts: int) -> int:
    """A filing whose facts have no resolvable basis -> one open item per filing."""
    payload = {"isin": isin, "filing_id": filing_id, "facts": facts}
    return queue_review(conn, ITEM_AMBIGUOUS_BASIS, payload, {"isin": isin, "filing_id": filing_id})


def queue_issue(
    conn: Connection, isin: str, filing_id: str, reason: str, key: str, detail: dict[str, Any]
) -> int:
    """A fact that cannot be normalized (unit, period, duplicate...) -> one item per key."""
    payload = {
        "isin": isin,
        "filing_id": filing_id,
        "reason": reason,
        "key": key,
        "detail": detail,
    }
    return queue_review(
        conn,
        ITEM_ISSUE,
        payload,
        {"isin": isin, "filing_id": filing_id, "reason": reason, "key": key},
    )


def load_resolutions(engine: Engine | None = None) -> Resolutions:
    """Resolved and ignored `unmapped_concept` items -> the mappings the mapper applies."""
    found = Resolutions()
    queue = tables.review_queue
    with transaction(engine) as conn:
        rows = conn.execute(
            sa.select(queue).where(
                queue.c.item_type == ITEM_UNMAPPED,
                queue.c.status.in_([STATUS_RESOLVED, STATUS_IGNORED]),
            )
        ).all()
    for row in rows:
        payload = json.loads(row.payload_json)
        key_type, key, family = payload["key_type"], payload["key"], payload["family"]
        scope = payload.get("statement") or ""
        if row.status == STATUS_IGNORED:
            if row.resolution_json and json.loads(row.resolution_json).get("auto_closed"):
                continue  # closed because the value was not needed then: evaluate it again
            if key_type == KEY_QNAME:
                found.ignored_qnames.add((family, key))
            else:
                found.ignored_labels.add((scope, key))
            continue
        concept = json.loads(row.resolution_json)["concept"]
        if key_type == KEY_QNAME:
            found.qnames[(family, key)] = concept
        elif key_type == KEY_LABEL:
            found.labels[(scope, key)] = concept
    return found


def _get_item(conn: Connection, item_id: int) -> sa.Row:
    """Item id -> row, or ReviewError."""
    row = conn.execute(
        sa.select(tables.review_queue).where(tables.review_queue.c.id == item_id)
    ).first()
    if row is None:
        raise ReviewError(f"no review item {item_id}")
    return row


def resolve(item_id: int, concept: str, taxonomy: Taxonomy, engine: Engine | None = None) -> None:
    """Map an open unmapped_concept item to a canonical concept; the next run applies it."""
    if concept not in taxonomy.concepts:
        raise ReviewError(f"unknown concept '{concept}'; see the taxonomy registry")
    with transaction(engine) as conn:
        row = _get_item(conn, item_id)
        if row.item_type != ITEM_UNMAPPED:
            raise ReviewError(
                f"item {item_id} is a {row.item_type}; only {ITEM_UNMAPPED} can be resolved"
            )
        if row.status != STATUS_OPEN:
            raise ReviewError(f"item {item_id} is already {row.status}")
        conn.execute(
            sa.update(tables.review_queue)
            .where(tables.review_queue.c.id == item_id)
            .values(
                status=STATUS_RESOLVED,
                resolution_json=json.dumps({"concept": concept}, sort_keys=True),
                updated_at=utc_now_iso(),
            )
        )


def ignore(item_id: int, engine: Engine | None = None, reason: str | None = None) -> None:
    """Mark an open item ignored (an ignored unmapped_concept is not queued again).

    The optional reason is stored with the item (`resolution_json`).
    """
    with transaction(engine) as conn:
        row = _get_item(conn, item_id)
        if row.status != STATUS_OPEN:
            raise ReviewError(f"item {item_id} is already {row.status}")
        conn.execute(
            sa.update(tables.review_queue)
            .where(tables.review_queue.c.id == item_id)
            .values(
                status=STATUS_IGNORED,
                resolution_json=json.dumps({"reason": reason}) if reason else None,
                updated_at=utc_now_iso(),
            )
        )


def close_not_needed(reports: Sequence[Any], engine: Engine | None = None) -> int:
    """Close open items that a normalize run found unnecessary -> how many were closed.

    Each report has `dropped` (NotNeeded: item type, match, reason) and `queued_unmapped`. An
    unmapped label is closed only if no filing of the run queued it. Closed items are `ignored`
    with the reason and `auto_closed`, so a later run looks at the label again.
    """
    queued: set[Any] = set()
    for report in reports:
        queued |= report.queued_unmapped
    queue = tables.review_queue
    closed = 0
    with transaction(engine) as conn:
        for report in reports:
            for need in report.dropped:
                if need.item_type == ITEM_UNMAPPED:
                    ident = tuple(need.match[k] for k in ("isin", "key", "key_type", "statement"))
                    if ident in queued:
                        continue
                rows = conn.execute(
                    sa.select(queue).where(
                        queue.c.item_type == need.item_type, queue.c.status == STATUS_OPEN
                    )
                ).all()
                for row in rows:
                    payload = json.loads(row.payload_json)
                    if all(payload.get(k) == v for k, v in need.match.items()):
                        conn.execute(
                            sa.update(queue)
                            .where(queue.c.id == row.id)
                            .values(
                                status=STATUS_IGNORED,
                                resolution_json=json.dumps(
                                    {"reason": need.reason, "auto_closed": True}, sort_keys=True
                                ),
                                updated_at=utc_now_iso(),
                            )
                        )
                        closed += 1
    return closed


def list_items(
    status: str | None = STATUS_OPEN, item_type: str | None = None, engine: Engine | None = None
) -> list[sa.Row]:
    """Review items filtered by status and type, oldest first."""
    queue = tables.review_queue
    query = sa.select(queue).order_by(queue.c.id)
    if status:
        query = query.where(queue.c.status == status)
    if item_type:
        query = query.where(queue.c.item_type == item_type)
    with transaction(engine) as conn:
        return list(conn.execute(query))
