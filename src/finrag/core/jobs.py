"""Job bookkeeping: one `jobs` row per pipeline stage run."""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.db import tables, transaction
from finrag.core.ids import PREFIX_JOB, new_id
from finrag.core.types import JobStatus, utc_now_iso


def start_job(job_type: str, isin: str | None = None, engine: Engine | None = None) -> str:
    """Job type (+ optional ISIN) -> new job_id, with a 'running' row written."""
    job_id = new_id(PREFIX_JOB)
    with transaction(engine) as conn:
        conn.execute(
            sa.insert(tables.jobs).values(
                job_id=job_id,
                job_type=job_type,
                isin=isin,
                status=JobStatus.RUNNING.value,
                started_at=utc_now_iso(),
            )
        )
    return job_id


def finish_job(
    job_id: str,
    status: JobStatus,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
    engine: Engine | None = None,
) -> None:
    """Record final status, finish time, stats counts and any error for a job."""
    with transaction(engine) as conn:
        result = conn.execute(
            sa.update(tables.jobs)
            .where(tables.jobs.c.job_id == job_id)
            .values(
                status=JobStatus(status).value,
                finished_at=utc_now_iso(),
                stats_json=json.dumps(stats or {}, default=str, sort_keys=True),
                error=error,
            )
        )
        if result.rowcount == 0:
            raise KeyError(f"unknown job_id: {job_id}")
