"""Corporate-event proposals from the cross_source classifier, and their confirmation.

A cluster that is explained as a restatement - the arithmetic of the PDF still holds
(`restated_comparative_likely`), or the filing's own note says the comparatives were restated
(`explained_by`, see cross_source.explain) - queues one `corporate_event_proposal` review item. It
changes nothing by itself: M05's values stay as they are. Confirming the item records a
`corporate_events` row (`comparative_restatement`, or `accounting_policy_change` when the
company's notes say so); M07 flags any metric whose periods straddle such an event, and shows a
growth figure on the restated comparative beside the one on the original. An event is never
recorded without one of the two explanations.

A native-text PDF comparative that differs from the original with neither explanation queues a
`comparative_disagreement` item instead: nothing is recorded and the original stays the value.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from finrag.core.db import tables, transaction
from finrag.core.types import EventType, utc_now_iso
from finrag.ingestion.registry import queue_review
from finrag.normalize.review_queue import STATUS_OPEN, STATUS_RESOLVED, ReviewError
from finrag.validation.cross_source import EXPLAINED_BY_IDENTITIES, RESTATED, Cluster

ITEM_EVENT_PROPOSAL = "corporate_event_proposal"
ITEM_DISAGREEMENT = "comparative_disagreement"
CONFIRMABLE = (EventType.ACCOUNTING_POLICY_CHANGE.value, EventType.COMPARATIVE_RESTATEMENT.value)


def event_bases(details: dict[str, Any]) -> list[str]:
    """The bases a recorded event concerns: `bases` if recorded, else its one `basis`."""
    found = details.get("bases") or [details.get("basis")]
    return [b for b in found if b]


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
        "explained_by": cluster.explained_by,
        "note": cluster.note,
        "identities": [e.as_dict() for e in cluster.evidence],
        "affected_keys": _affected(cluster),
    }


def disagreement_payload(isin: str, cluster: Cluster, rule_id: str) -> dict[str, Any]:
    """A native-text comparative that differs from the original with no explanation -> payload."""
    return {
        "isin": isin,
        "basis": cluster.basis,
        "filing_id": cluster.pdf_filing_id,
        "restated_period_end": cluster.period_end.isoformat(),
        "rule_id": rule_id,
        "summary": (
            f"{cluster.pdf_filing_id} prints {len(cluster.differences)} comparative figures of "
            f"the period ending {cluster.period_end} that differ from the original filing, with "
            "no restatement note and no identity that holds to explain them"
        ),
        "classification": cluster.classification,
        "reasons": cluster.reasons,
        "identities": [e.as_dict() for e in cluster.evidence],
        "differences": [d.as_dict() for d in cluster.differences],
    }


def _same_values(event_details: dict[str, Any], cluster: Cluster, rounding: int) -> bool:
    """True if every differing key of the cluster is a key of the event with the same PDF value.

    An event without a list of affected keys is matched on basis and restated period alone.
    """
    keys = event_details.get("affected_keys")
    if not keys:
        return True
    known = {(k["concept"], k["period_start"], k["period_end"]): Decimal(k["pdf"]) for k in keys}
    for d in cluster.differences:
        value = known.get((d.concept, d.period.start.isoformat(), d.period.end.isoformat()))
        if value is None or abs(value - d.other.value) > rounding:
            return False
    return True


def explaining_event(
    conn: Connection, isin: str, cluster: Cluster, rounding: int = 10_000
) -> sa.Row | None:
    """A recorded event that already explains this restated cluster, or None.

    Same company, same basis, and either the filing that printed the cluster is the one the event
    was confirmed from (or is linked to it): one restatement shows in every comparative column
    of that filing; or the event's restated period end is the cluster's and the PDF values of the
    cluster are the values the event recorded (the same restatement shown again by another
    filing: Netweb's FY25 comparatives appear in the Q4 FY26 and in the Q2 FY26 results).
    """
    events = tables.corporate_events
    for row in conn.execute(
        sa.select(events)
        .where(events.c.isin == isin, events.c.event_type.in_(list(CONFIRMABLE)))
        .order_by(events.c.id)
    ):
        details = json.loads(row.details or "{}")
        if cluster.basis not in event_bases(details):
            continue
        filings = {details.get("pdf_filing_id")} | {
            link.get("pdf_filing_id") for link in details.get("linked_filings", [])
        }
        if cluster.pdf_filing_id in filings:
            return row
        if details.get("restated_period_end") == cluster.period_end.isoformat() and _same_values(
            details, cluster, rounding
        ):
            return row
    return None


def link_cluster(conn: Connection, event: sa.Row, cluster: Cluster, rule_id: str) -> None:
    """Attach a cluster to the event that explains it (the event's details list the filings)."""
    details = json.loads(event.details or "{}")
    linked = details.setdefault("linked_filings", [])
    entry = {
        "pdf_filing_id": cluster.pdf_filing_id,
        "period_end": cluster.period_end.isoformat(),
        "differing_keys": len(cluster.differences),
        "rule_id": rule_id,
    }
    if entry["pdf_filing_id"] == details.get("pdf_filing_id") or entry in linked:
        return
    linked.append(entry)
    conn.execute(
        sa.update(tables.corporate_events)
        .where(tables.corporate_events.c.id == event.id)
        .values(details=json.dumps(details, sort_keys=True))
    )


def link_item(item_id: int, event_id: int, engine: Engine | None = None) -> None:
    """Close an open proposal as resolved by linking it to a recorded event."""
    with transaction(engine) as conn:
        item = conn.execute(
            sa.select(tables.review_queue).where(tables.review_queue.c.id == item_id)
        ).first()
        event = conn.execute(
            sa.select(tables.corporate_events).where(tables.corporate_events.c.id == event_id)
        ).first()
        if item is None or event is None:
            raise ReviewError(f"no review item {item_id} or no event {event_id}")
        if item.item_type != ITEM_EVENT_PROPOSAL or item.status != STATUS_OPEN:
            raise ReviewError(f"item {item_id} is not an open {ITEM_EVENT_PROPOSAL}")
        payload = json.loads(item.payload_json)
        if payload["isin"] != event.isin:
            raise ReviewError(f"item {item_id} and event {event_id} are of different companies")
        if not explanations(payload):
            raise ReviewError(f"item {item_id} has neither a restatement note nor identities")
        _add_basis(conn, event, payload)
        _resolve_linked(conn, item.id, event.id, "linked by hand")


def _add_basis(conn: Connection, event: sa.Row, payload: dict[str, Any]) -> None:
    """A proposal for another basis of the same filing extends the event to that basis: a scheme
    of arrangement restates the standalone and the consolidated statements alike."""
    details = json.loads(event.details or "{}")
    bases = event_bases(details)
    if payload["basis"] in bases:
        return
    details["bases"] = [*bases, payload["basis"]]
    linked = details.setdefault("linked_filings", [])
    entry = {
        "pdf_filing_id": payload["filing_id"],
        "basis": payload["basis"],
        "period_end": payload["restated_period_end"],
        "differing_keys": len(payload.get("affected_keys", [])),
        "rule_id": payload.get("rule_id"),
    }
    if entry not in linked and payload["filing_id"] != details.get("pdf_filing_id"):
        linked.append(entry)
    conn.execute(
        sa.update(tables.corporate_events)
        .where(tables.corporate_events.c.id == event.id)
        .values(details=json.dumps(details, sort_keys=True))
    )


def _resolve_linked(conn: Connection, item_id: int, event_id: int, how: str) -> None:
    """Mark a proposal resolved by an existing event."""
    conn.execute(
        sa.update(tables.review_queue)
        .where(tables.review_queue.c.id == item_id)
        .values(
            status=STATUS_RESOLVED,
            resolution_json=json.dumps({"linked_event": event_id, "how": how}, sort_keys=True),
            updated_at=utc_now_iso(),
        )
    )


def propose_event(
    conn: Connection, isin: str, cluster: Cluster, rule_id: str, rounding: int = 10_000
) -> int | None:
    """Queue a proposal for a restated cluster, unless a recorded event already explains it
    (the cluster is then attached to that event) or one was already decided (not re-queued)."""
    payload = proposal_payload(isin, cluster, rule_id)
    match = _identity_of(payload)
    queue = tables.review_queue
    event = explaining_event(conn, isin, cluster, rounding)
    if event is not None:
        link_cluster(conn, event, cluster, rule_id)
        for row in conn.execute(sa.select(queue).where(queue.c.item_type == ITEM_EVENT_PROPOSAL)):
            existing = json.loads(row.payload_json)
            if row.status == STATUS_OPEN and all(existing.get(k) == v for k, v in match.items()):
                _resolve_linked(conn, row.id, event.id, "same restatement as the recorded event")
        return None
    for row in conn.execute(sa.select(queue).where(queue.c.item_type == ITEM_EVENT_PROPOSAL)):
        existing = json.loads(row.payload_json)
        if row.status != STATUS_OPEN and all(existing.get(k) == v for k, v in match.items()):
            return None
    return queue_review(conn, ITEM_EVENT_PROPOSAL, payload, match)


def propose_disagreement(conn: Connection, isin: str, cluster: Cluster, rule_id: str) -> int | None:
    """Queue a comparative that disagrees with its original; once decided, it is not re-queued."""
    payload = disagreement_payload(isin, cluster, rule_id)
    match = _identity_of(payload)
    queue = tables.review_queue
    for row in conn.execute(sa.select(queue).where(queue.c.item_type == ITEM_DISAGREEMENT)):
        existing = json.loads(row.payload_json)
        if row.status != STATUS_OPEN and all(existing.get(k) == v for k, v in match.items()):
            return None
    return queue_review(conn, ITEM_DISAGREEMENT, payload, match)


def explanations(payload: dict[str, Any]) -> list[str]:
    """What explains a proposal: its recorded `explained_by`, else the classification (older
    proposals recorded only that the identities held)."""
    found = list(payload.get("explained_by") or [])
    if not found and payload.get("classification") == RESTATED:
        found.append(EXPLAINED_BY_IDENTITIES)
    return found


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
        if not explanations(payload):
            raise ReviewError(
                f"item {item_id} has neither a restatement note nor identities that hold: "
                "an event needs one of them"
            )
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
            "explained_by": explanations(payload),
            "note": payload.get("note"),
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
