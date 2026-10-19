"""The current view: every later module reads facts only through this file.

For each (isin, concept, period, basis) the current fact is the best-ranked one (see
`Fact.rank`: XBRL first, then XBRL-only derived, PDF/OCR, other derived), and within a rank the
latest by filing date, `revision_no`, then `created_at`. Facts of quarantined filings are never a
source. `as_of` returns what was known on that date.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from finrag.core.db import tables, transaction
from finrag.core.types import FilingStatus, Period, PeriodType, SourceClass
from finrag.facts.gap_gate import REASON_INPUT_REJECTED, rejected_fact_ids
from finrag.facts.models import Fact
from finrag.normalize.taxonomy import placeholder_guards

c_ = tables.canonical_facts


def _select() -> sa.Select:
    """canonical_facts joined to its filing (date, revision) and parsed fact (confidence)."""
    c, f, p = tables.canonical_facts, tables.filings, tables.parsed_facts
    return (
        sa.select(
            c,
            f.c.filed_on.label("filing_filed_on"),
            f.c.revision_no.label("filing_revision_no"),
            p.c.confidence.label("confidence"),
        )
        .select_from(
            c.join(f, f.c.filing_id == c.c.source_filing_id).outerjoin(
                p, p.c.id == c.c.parsed_fact_id
            )
        )
        .where(f.c.status != FilingStatus.QUARANTINED.value)
    )


def _to_fact(row: sa.Row) -> Fact:
    """Joined row -> Fact."""
    start = date.fromisoformat(row.period_start or row.period_end)
    return Fact(
        fact_id=row.fact_id,
        isin=row.isin,
        concept=row.concept,
        period=Period(start, date.fromisoformat(row.period_end), PeriodType(row.period_type)),
        basis=row.basis,
        value=Decimal(row.value),
        unit=row.unit,
        source_filing_id=row.source_filing_id,
        parsed_fact_id=row.parsed_fact_id,
        mapping_method=row.mapping_method,
        is_derived=bool(row.is_derived),
        derivation=json.loads(row.derivation_json) if row.derivation_json else None,
        stored_filed_on=row.filed_on,
        created_at=row.created_at,
        source_class=SourceClass(row.source_class),
        filed_on=row.filing_filed_on,
        revision_no=row.filing_revision_no or 0,
        confidence=row.confidence,
    )


def load_facts(
    conn: Connection,
    isin: str,
    concepts: Sequence[str] | None = None,
    basis: str | None = None,
    periods: Sequence[Period] | None = None,
    include_rejected: bool = False,
) -> list[Fact]:
    """Every stored fact (all versions, quarantined filings excluded) matching the filters.

    Facts the gap-fill gate refused (and derived facts built on them) are left out, unless
    `include_rejected` is true; then they are returned with `gap_rejected` set.
    """
    c = tables.canonical_facts
    query = _select().where(c.c.isin == isin)
    if concepts is not None:
        query = query.where(c.c.concept.in_(list(concepts)))
    if basis is not None:
        query = query.where(c.c.basis == basis)
    if periods is not None:
        query = query.where(c.c.period_end.in_(sorted({p.end.isoformat() for p in periods})))
    facts = [_to_fact(r) for r in conn.execute(query.order_by(c.c.created_at, c.c.fact_id))]
    facts = _without_placeholders(conn, isin, facts)
    facts = _mark_rejected(conn, isin, facts, include_rejected)
    if periods is not None:
        wanted = {(p.start, p.end) for p in periods}
        facts = [f for f in facts if (f.period.start, f.period.end) in wanted]
    return facts


def _mark_rejected(
    conn: Connection, isin: str, facts: list[Fact], include_rejected: bool
) -> list[Fact]:
    """Flag (or drop) the facts the gate refused, and derived facts that used one."""
    refused = rejected_fact_ids(conn, isin)
    if not refused:
        return facts
    marked = []
    for fact in facts:
        reason = refused.get(fact.fact_id)
        if reason is None and fact.is_derived:
            used = set((fact.derivation or {}).get("input_fact_ids", []))
            if used & set(refused):
                reason = REASON_INPUT_REJECTED
        if reason is None:
            marked.append(fact)
        elif include_rejected:
            marked.append(dataclasses.replace(fact, gap_rejected=True, gap_reason=reason))
    return marked


def _without_placeholders(conn: Connection, isin: str, facts: list[Fact]) -> list[Fact]:
    """Hide stored zeros that the taxonomy calls placeholders, and facts derived from them.

    `zero_is_placeholder_while_nonzero`: a 0 of the concept is "not tagged" while the guard
    concept (e.g. profit for the period) is non-zero for the same basis and period. The normalizer
    no longer stores such zeros; ones stored earlier stay in the append-only table and are hidden
    here, together with any derived fact that used one (it inherited the placeholder). The
    placeholders are looked up across all periods, so a filtered query (one period) hides a derived
    fact whose placeholder input lies in another period.
    """
    guards = placeholder_guards()
    if not guards or not any(f.concept in guards for f in facts):
        return facts
    query = _select().where(
        c_.c.isin == isin, c_.c.concept.in_(sorted({*guards, *guards.values()}))
    )
    stored = [_to_fact(r) for r in conn.execute(query)]
    grouped: dict[tuple[str, str], list[Fact]] = {}
    for g in stored:
        grouped.setdefault((g.basis, g.concept), []).append(g)
    current = {
        (basis, concept, start, end): top
        for (basis, concept), versions in grouped.items()
        for (start, end), top in current_of(versions).items()
    }
    hidden = set()
    for f in stored:
        guard_name = guards.get(f.concept)
        if guard_name is None or f.is_derived or f.value != 0:
            continue
        guard = current.get((f.basis, guard_name, f.period.start, f.period.end))
        if guard is not None and guard.value != 0:
            hidden.add(f.fact_id)
    if not hidden:
        return facts
    return [
        f
        for f in facts
        if f.fact_id not in hidden
        and not (f.is_derived and hidden & set((f.derivation or {}).get("input_fact_ids", [])))
    ]


def best(facts: Iterable[Fact]) -> Fact | None:
    """The current fact among versions of one key: lowest rank, then latest by `Fact.order`."""
    return max(facts, key=lambda f: (-f.rank, f.order, f.fact_id), default=None)


def current_of(
    facts: Iterable[Fact], as_of: date | None = None, reported_only: bool = False
) -> dict[tuple[date, date], Fact]:
    """Versions of one (isin, concept, basis) -> the current fact per (start, end)."""
    groups: dict[tuple[date, date], list[Fact]] = {}
    for fact in facts:
        if as_of is not None and fact.known_on > as_of.isoformat():
            continue
        if reported_only and fact.is_derived:
            continue
        groups.setdefault((fact.period.start, fact.period.end), []).append(fact)
    return {key: top for key, versions in groups.items() if (top := best(versions))}


def get_fact(
    isin: str,
    concept: str,
    period: Period,
    basis: str,
    as_of: date | None = None,
    engine: Engine | None = None,
) -> Fact | None:
    """One key -> its current fact (None if there is none, or none was known by `as_of`)."""
    with transaction(engine) as conn:
        found = load_facts(conn, isin, [concept], basis, [period])
    return current_of(found, as_of).get((period.start, period.end))


def get_facts(
    isin: str,
    concepts: Sequence[str] | None = None,
    periods: Sequence[Period] | None = None,
    basis: str | None = None,
    as_of: date | None = None,
    engine: Engine | None = None,
) -> list[Fact]:
    """Filters -> the current fact of every matching key, ordered by concept, basis, period."""
    with transaction(engine) as conn:
        found = load_facts(conn, isin, concepts, basis, periods)
    grouped: dict[tuple[str, str], list[Fact]] = {}
    for fact in found:
        grouped.setdefault((fact.concept, fact.basis), []).append(fact)
    out = [f for versions in grouped.values() for f in current_of(versions, as_of).values()]
    return sorted(out, key=lambda f: (f.concept, f.basis, f.period.end, f.period.start))


def get_series(
    isin: str,
    concept: str,
    period_type: PeriodType,
    basis: str,
    n: int,
    as_of: date | None = None,
    engine: Engine | None = None,
) -> list[Fact]:
    """The latest `n` current facts of one period type, oldest first."""
    facts = [
        f
        for f in get_facts(isin, [concept], None, basis, as_of, engine)
        if f.period.type is period_type
    ]
    return facts[-n:] if n > 0 else []


def history(
    isin: str, concept: str, period: Period, basis: str, engine: Engine | None = None
) -> list[Fact]:
    """Every stored version of one key, oldest written first."""
    with transaction(engine) as conn:
        return load_facts(conn, isin, [concept], basis, [period])
