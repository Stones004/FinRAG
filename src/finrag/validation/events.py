"""Corporate-event proposals from the cross_source classifier, and their confirmation.

A `restated_comparative_likely` cluster queues one `corporate_event_proposal` review item. It
changes nothing by itself: M05's values stay as they are. Confirming the item records a
`corporate_events` row (`comparative_restatement`, or `accounting_policy_change` when the
company's notes say so); M07 flags any metric whose periods straddle such an event.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from finrag.core.db import tables, transaction
from finrag.core.types import EventType, utc_now_iso
from finrag.ingestion.registry import queue_review
from finrag.normalize.review_queue import STATUS_OPEN, STATUS_RESOLVED, ReviewError
from finrag.validation.cross_source import Cluster

ITEM_EVENT_PROPOSAL = "corporate_event_proposal"
CONFIRMABLE = (EventType.ACCOUNTING_POLICY_CHANGE.value, EventType.COMPARATIVE_RESTATEMENT.value)


def _identity_of(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload fields that make one proposal the same as another."""
    return {k: payload[k] for k in ("isin", "basis", "filing_id", "restated_period_end")}


def _affected(cluster: Cluster) -> list[dict[str, Any]]:
    """Differing keys, compactly."""
    return [
        {
            "concept": d.concept,
            "period_start": d.period.start.isoformat(),
            "period_end": d.period.end.isoformat(),
            "period_type": d.period.type.value,
            "xbrl": format(d.xbrl.value, "f"),
            "pdf": format(d.other.value, "f"),
        }
        for d in cluster.differences
    ]


def proposal_payload(isin: str, cluster: Cluster, rule_id: str) -> dict[str, Any]:
    """A restated-comparative cluster -> the review item payload."""
    ends = cluster.period_end
    return {
        "isin": isin,
        "basis": cluster.basis,
        "filing_id": cluster.pdf_filing_id,
        "restated_period_end": ends.isoformat(),
        "proposed_event_type": EventType.COMPARATIVE_RESTATEMENT.value,
        "proposed_event_date": (ends + timedelta(days=1)).isoformat(),
        "rule_id": rule_id,
        "summary": (
            f"{cluster.pdf_filing_id} restates {len(cluster.differences)} lines of the period "
            f"ending {ends} against the XBRL values"
        ),
        "classification": cluster.classification,
        "reasons": cluster.reasons,
        "identities": [e.as_dict() for e in cluster.evidence],
        "affected_keys": _affected(cluster),
    }


def propose_event(conn: Connection, isin: str, cluster: Cluster, rule_id: str) -> int | None:
    """Queue a proposal for a restated cluster; None if one was already decided (not re-queued)."""
    payload = proposal_payload(isin, cluster, rule_id)
    match = _identity_of(payload)
    queue = tables.review_queue
    for row in conn.execute(sa.select(queue).where(queue.c.item_type == ITEM_EVENT_PROPOSAL)):
        existing = json.loads(row.payload_json)
        if row.status != STATUS_OPEN and all(existing.get(k) == v for k, v in match.items()):
            return None
    return queue_review(conn, ITEM_EVENT_PROPOSAL, payload, match)


def confirm_event(
    item_id: int,
    event_type: str | None = None,
    event_date: date | None = None,
    citation: str | None = None,
    engine: Engine | None = None,
) -> int:
    """Confirm a proposal: record the corporate event, resolve the item -> the event's id."""
    with transaction(engine) as conn:
        item = conn.execute(
            sa.select(tables.review_queue).where(tables.review_queue.c.id == item_id)
        ).first()
        if item is None:
            raise ReviewError(f"no review item {item_id}")
        if item.item_type != ITEM_EVENT_PROPOSAL:
            raise ReviewError(f"item {item_id} is a {item.item_type}, not a {ITEM_EVENT_PROPOSAL}")
        if item.status != STATUS_OPEN:
            raise ReviewError(f"item {item_id} is already {item.status}")
        payload = json.loads(item.payload_json)
        kind = event_type or payload["proposed_event_type"]
        if kind not in CONFIRMABLE:
            raise ReviewError(f"event type must be one of {', '.join(CONFIRMABLE)}, not '{kind}'")
        when = (event_date or date.fromisoformat(payload["proposed_event_date"])).isoformat()
        events = tables.corporate_events
        same = conn.execute(
            sa.select(events.c.id).where(
                events.c.isin == payload["isin"],
                events.c.event_type == kind,
                events.c.event_date == when,
            )
        ).first()
        if same is not None:
            raise ReviewError(f"{kind} on {when} is already recorded (event {same.id})")
        details = {
            "basis": payload["basis"],
            "pdf_filing_id": payload["filing_id"],
            "restated_period_end": payload["restated_period_end"],
            "citation": citation,
            "classification": payload["classification"],
            "proposal_item": item_id,
            "affected_keys": payload["affected_keys"],
        }
        event_id = int(
            conn.execute(
                sa.insert(events).values(
                    isin=payload["isin"],
                    event_type=kind,
                    event_date=when,
                    details=json.dumps(details, sort_keys=True),
                    source=f"validation:{payload['rule_id']}:item{item_id}",
                )
            ).inserted_primary_key[0]
        )
        conn.execute(
            sa.update(tables.review_queue)
            .where(tables.review_queue.c.id == item_id)
            .values(
                status=STATUS_RESOLVED,
                resolution_json=json.dumps(
                    {
                        "event_id": event_id,
                        "event_type": kind,
                        "event_date": when,
                        "citation": citation,
                    },
                    sort_keys=True,
                ),
                updated_at=utc_now_iso(),
            )
        )
    return event_id


def recorded_events(
    isin: str, event_types: tuple[str, ...] = CONFIRMABLE, engine: Engine | None = None
) -> list[sa.Row]:
    """Recorded events of these types for a company, by date."""
    events = tables.corporate_events
    with transaction(engine) as conn:
        return list(
            conn.execute(
                sa.select(events)
                .where(events.c.isin == isin, events.c.event_type.in_(list(event_types)))
                .order_by(events.c.event_date, events.c.id)
            )
        )
