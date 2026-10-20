"""Which periods a company has data for: the question resolvers' view of the database."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.db import tables, transaction
from finrag.core.fiscal import fiscal_year_of, label_to_period
from finrag.core.types import MetricStatus, Period, PeriodType

# When two periods end on the same day, the one listed first is shown (a Q4 and an FY end together).
TYPE_PREFERENCE = (PeriodType.Q, PeriodType.FY, PeriodType.NINE_M, PeriodType.H)


class Availability(Protocol):
    """What the resolvers need to know about the stored data (stubbed in tests)."""

    def has_period(self, isin: str, period: Period) -> bool:
        """True if the company has any stored value for exactly this period."""
        ...

    def latest(
        self, isin: str, metric_ids: Iterable[str], concept_ids: Iterable[str]
    ) -> Period | None:
        """The latest period with data for these metrics/concepts (all data if none are given)."""
        ...

    def latest_transcript(self, isin: str) -> Period | None:
        """The latest period with an indexed earnings-call transcript (None: no transcript)."""
        ...


def _rank(period: Period) -> tuple[date, int]:
    """Sort key: later end first, then the preferred type."""
    return (period.end, -TYPE_PREFERENCE.index(period.type))


def duration_for_instant(day: date) -> Period | None:
    """A balance-sheet date -> the fiscal period that ends on it (FY, else H1/9M/quarter)."""
    year = fiscal_year_of(day)
    yy = f"{year % 100:02d}"
    for label in (f"FY{yy}", f"H1 FY{yy}", f"9M FY{yy}", *(f"Q{i} FY{yy}" for i in (1, 2, 3, 4))):
        period = label_to_period(label)
        if period.end == day:
            return period
    return None


def best_period(periods: Iterable[Period]) -> Period | None:
    """The latest period; ties go to the preferred type."""
    return max(periods, key=_rank, default=None)


class DbAvailability:
    """Availability from metric_values and canonical_facts."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    def has_period(self, isin: str, period: Period) -> bool:
        """True if canonical_facts holds any fact for exactly this period."""
        c = tables.canonical_facts
        query = sa.select(c.c.fact_id).where(
            c.c.isin == isin,
            c.c.period_start == period.start.isoformat(),
            c.c.period_end == period.end.isoformat(),
            c.c.period_type == period.type.value,
        )
        with transaction(self._engine) as conn:
            return conn.execute(query.limit(1)).first() is not None

    def latest(
        self, isin: str, metric_ids: Iterable[str], concept_ids: Iterable[str]
    ) -> Period | None:
        """Latest duration period with a computed metric value or a fact (any, if none named)."""
        metrics, concepts = list(metric_ids), list(concept_ids)
        found: list[Period] = []
        with transaction(self._engine) as conn:
            if metrics or not concepts:
                m = tables.metric_values
                query = sa.select(m.c.period_start, m.c.period_end, m.c.period_type).where(
                    m.c.isin == isin, m.c.status == MetricStatus.COMPUTED.value
                )
                if metrics:
                    query = query.where(m.c.metric_id.in_(metrics))
                found += self._periods(conn.execute(query.distinct()))
            if concepts or not metrics:
                c = tables.canonical_facts
                query = sa.select(c.c.period_start, c.c.period_end, c.c.period_type).where(
                    c.c.isin == isin
                )
                if concepts:
                    query = query.where(c.c.concept.in_(concepts))
                found += self._periods(conn.execute(query.distinct()))
        return best_period(found)

    def latest_transcript(self, isin: str) -> Period | None:
        """The latest period of the company's indexed transcript passages."""
        c = tables.chunks
        query = sa.select(c.c.period_start, c.c.period_end, c.c.period_type).where(
            c.c.isin == isin,
            c.c.source_type == "transcript",
            c.c.period_start.is_not(None),
            c.c.period_end.is_not(None),
        )
        with transaction(self._engine) as conn:
            return best_period(self._periods(conn.execute(query.distinct())))

    @staticmethod
    def _periods(rows: Iterable[sa.Row]) -> list[Period]:
        """Rows -> duration periods (a balance-sheet date becomes the period ending on it)."""
        out: list[Period] = []
        for row in rows:
            if row.period_type == PeriodType.INSTANT.value:
                mapped = duration_for_instant(date.fromisoformat(row.period_end))
                if mapped is not None:
                    out.append(mapped)
            else:
                out.append(
                    Period(
                        date.fromisoformat(row.period_start),
                        date.fromisoformat(row.period_end),
                        PeriodType(row.period_type),
                    )
                )
        return out


class StubAvailability:
    """Availability from a fixed list of periods (tests and the offline routing run)."""

    def __init__(
        self,
        periods: Iterable[Period],
        by_metric: dict[str, list[Period]] | None = None,
        transcripts: Iterable[Period] = (),
    ):
        self.periods = list(periods)
        self.by_metric = by_metric or {}
        self.transcripts = list(transcripts)

    def latest_transcript(self, isin: str) -> Period | None:
        """Latest listed transcript period (None when the stub lists none)."""
        return best_period(self.transcripts)

    def has_period(self, isin: str, period: Period) -> bool:
        """True if the period is in the list."""
        return period in self.periods

    def latest(
        self, isin: str, metric_ids: Iterable[str], concept_ids: Iterable[str]
    ) -> Period | None:
        """Latest listed period (restricted per metric when `by_metric` names the metrics)."""
        named = [m for m in metric_ids if m in self.by_metric]
        pool = [p for m in named for p in self.by_metric[m]] if named else self.periods
        return best_period(pool)
