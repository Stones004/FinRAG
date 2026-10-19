"""What a new or restated fact touches: derived facts built on it, and periods to re-validate."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.db import tables, transaction
from finrag.core.types import PeriodType
from finrag.facts.current_view import load_facts
from finrag.facts.derivation import Group, target_periods_fed_by


class PeriodKey(NamedTuple):
    """An (isin, period, basis) that M06/M07 must recompute."""

    isin: str
    period_start: date
    period_end: date
    period_type: PeriodType
    basis: str


@dataclass(frozen=True)
class AffectedSet:
    """Derived facts that depend on the given facts, periods to recompute, groups to re-derive."""

    derived_fact_ids: tuple[str, ...]
    periods: frozenset[PeriodKey]
    rederive: frozenset[Group]


def affected(fact_ids: Iterable[str], engine: Engine | None = None) -> AffectedSet:
    """Fact IDs -> the derived facts using any version of the same key, and what to recompute.

    A derived fact points at the input fact IDs it used, so a restated input (a new ID for the
    same key) is matched through its older versions. `periods` holds the facts' own periods, their
    dependants' periods and the periods that could now be derived from them.
    """
    ids = set(fact_ids)
    c = tables.canonical_facts
    derived: set[str] = set()
    periods: set[PeriodKey] = set()
    groups: set[Group] = set()
    with transaction(engine) as conn:
        given = conn.execute(sa.select(c).where(c.c.fact_id.in_(sorted(ids)))).all()
        for row in given:
            group = (row.isin, row.concept, row.basis)
            groups.add(group)
            versions = load_facts(conn, row.isin, [row.concept], row.basis, None, True)
            same_key = {
                v.fact_id
                for v in versions
                if (v.period.start.isoformat(), v.period.end.isoformat())
                == (row.period_start, row.period_end)
            }
            period = next(v.period for v in versions if v.fact_id == row.fact_id)
            periods.add(PeriodKey(row.isin, period.start, period.end, period.type, row.basis))
            for other in versions:
                uses = set((other.derivation or {}).get("input_fact_ids", []))
                if other.is_derived and uses & same_key:
                    derived.add(other.fact_id)
                    p = other.period
                    periods.add(PeriodKey(row.isin, p.start, p.end, p.type, row.basis))
            if period.type is PeriodType.INSTANT:
                continue
            for target in target_periods_fed_by(period):
                periods.add(PeriodKey(row.isin, target.start, target.end, target.type, row.basis))
    return AffectedSet(tuple(sorted(derived)), frozenset(periods), frozenset(groups))
