"""Restatements: the difference between two values of one fact, flagged against tolerances."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from finrag.core.config import Settings
from finrag.core.db import tables
from finrag.core.types import utc_now_iso
from finrag.facts.models import Fact

UNIT_INR = "INR"


@dataclass(frozen=True)
class Difference:
    """abs and relative difference between an old and a new fact, and whether it is flagged."""

    abs_diff: Decimal
    rel_diff: Decimal | None  # None when the old value is 0
    flagged: bool


def assess(old: Fact, new: Fact, settings: Settings) -> Difference:
    """Old and new fact -> Difference; flagged only if every applicable tolerance is exceeded.

    The absolute tolerance is in rupees, so it applies to INR concepts only; per-share and ratio
    concepts are judged on the relative tolerance alone. An old value of 0 filled in later is
    flagged for an INR concept (money appeared) but not for a per-share or ratio concept.
    """
    tol = settings.restatement
    abs_diff = abs(new.value - old.value)
    rel_diff = None if old.value == 0 else abs_diff / abs(old.value)
    if rel_diff is None and new.unit != UNIT_INR:
        # a per-share or ratio figure that was 0 and is now filled in is a tag correction (a
        # revision that adds EPS or face value), not a change of what the company reported
        return Difference(abs_diff, None, False)
    rel_exceeded = rel_diff is None or rel_diff > Decimal(str(tol.relative_tolerance))
    abs_exceeded = new.unit != UNIT_INR or abs_diff > Decimal(tol.absolute_tolerance_inr)
    return Difference(abs_diff, rel_diff, bool(abs_diff) and rel_exceeded and abs_exceeded)


def record(conn: Connection, old: Fact, new: Fact, settings: Settings) -> int | None:
    """Write a `restatements` row for old -> new; None if that pair is already recorded."""
    r = tables.restatements
    seen = conn.execute(
        sa.select(r.c.id).where(r.c.old_fact_id == old.fact_id, r.c.new_fact_id == new.fact_id)
    ).first()
    if seen is not None:
        return None
    diff = assess(old, new, settings)
    result = conn.execute(
        sa.insert(r).values(
            isin=new.isin,
            concept=new.concept,
            period_start=new.period.start.isoformat(),
            period_end=new.period.end.isoformat(),
            basis=new.basis,
            old_fact_id=old.fact_id,
            new_fact_id=new.fact_id,
            abs_diff=str(diff.abs_diff),
            rel_diff=None if diff.rel_diff is None else str(diff.rel_diff),
            flagged=int(diff.flagged),
            created_at=utc_now_iso(),
        )
    )
    return int(result.inserted_primary_key[0])
