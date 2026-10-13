"""Read stored metric values: the latest row per (company, metric, period, basis).

`settings.metrics.version_policy` decides which formula version is read: `latest` takes the newest
row written for the key; `pinned` takes the newest row of the version pinned for that metric in
`settings.metrics.pinned_versions` (a metric without a pin follows `latest`).
"""

from __future__ import annotations

import json
from datetime import date

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import get_settings
from finrag.core.db import tables, transaction
from finrag.core.types import MetricStatus, Period, PeriodType, text_to_decimal
from finrag.metrics.models import MetricValue


def _to_value(row: sa.Row) -> MetricValue:
    """metric_values row -> MetricValue."""
    start = date.fromisoformat(row.period_start)
    return MetricValue(
        metric_value_id=row.metric_value_id,
        isin=row.isin,
        metric_id=row.metric_id,
        formula_version=row.formula_version,
        period=Period(start, date.fromisoformat(row.period_end), PeriodType(row.period_type)),
        basis=row.basis,
        value=text_to_decimal(row.value) if row.value is not None else None,
        status=MetricStatus(row.status),
        reason=row.reason or "",
        details=json.loads(row.details_json) if row.details_json else {},
        input_fact_ids=tuple(json.loads(row.input_fact_ids_json or "[]")),
        run_id=row.run_id,
        created_at=row.created_at,
    )


def _pinned(metric_id: str) -> str | None:
    """The version to read for a metric under the pinned policy (None = newest)."""
    metrics = get_settings().metrics
    return metrics.pinned_versions.get(metric_id) if metrics.version_policy == "pinned" else None


def _newest(rows: list[sa.Row]) -> MetricValue | None:
    """The newest row (metric_value_id is a time-sortable ULID)."""
    return _to_value(max(rows, key=lambda r: r.metric_value_id)) if rows else None


def get_metric(
    isin: str,
    metric_id: str,
    period: Period,
    basis: str,
    engine: Engine | None = None,
) -> MetricValue | None:
    """The stored value of a metric for a company, period and requested basis (None: never run)."""
    m = tables.metric_values
    query = sa.select(m).where(
        m.c.isin == isin,
        m.c.metric_id == metric_id,
        m.c.period_start == period.start.isoformat(),
        m.c.period_end == period.end.isoformat(),
        m.c.period_type == period.type.value,
        m.c.basis == basis,
    )
    pin = _pinned(metric_id)
    if pin is not None:
        query = query.where(m.c.formula_version == pin)
    with transaction(engine) as conn:
        return _newest(list(conn.execute(query)))


def get_metric_series(
    isin: str,
    metric_id: str,
    basis: str,
    period_type: PeriodType,
    n: int,
    engine: Engine | None = None,
) -> list[MetricValue]:
    """The latest `n` periods of a metric (one value per period), oldest first."""
    m = tables.metric_values
    query = sa.select(m).where(
        m.c.isin == isin,
        m.c.metric_id == metric_id,
        m.c.period_type == period_type.value,
        m.c.basis == basis,
    )
    pin = _pinned(metric_id)
    if pin is not None:
        query = query.where(m.c.formula_version == pin)
    with transaction(engine) as conn:
        rows = list(conn.execute(query))
    by_period: dict[tuple[str, str], list[sa.Row]] = {}
    for row in rows:
        by_period.setdefault((row.period_end, row.period_start), []).append(row)
    values = [v for key in sorted(by_period) if (v := _newest(by_period[key])) is not None]
    return values[-n:] if n > 0 else []


def latest_values(
    isin: str, metric_id: str, basis: str | None = None, engine: Engine | None = None
) -> list[MetricValue]:
    """The latest value of a metric for every stored period and basis, ordered by basis and date."""
    m = tables.metric_values
    query = sa.select(m).where(m.c.isin == isin, m.c.metric_id == metric_id)
    if basis is not None:
        query = query.where(m.c.basis == basis)
    pin = _pinned(metric_id)
    if pin is not None:
        query = query.where(m.c.formula_version == pin)
    with transaction(engine) as conn:
        rows = list(conn.execute(query))
    grouped: dict[tuple[str, str, str, str], list[sa.Row]] = {}
    for row in rows:
        key = (row.basis, row.period_end, row.period_type, row.period_start)
        grouped.setdefault(key, []).append(row)
    return [v for key in sorted(grouped) if (v := _newest(grouped[key])) is not None]
