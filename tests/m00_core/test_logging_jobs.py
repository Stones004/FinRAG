import json

import pytest
import sqlalchemy as sa

from finrag.core.config import get_settings
from finrag.core.db import init_db, tables, transaction
from finrag.core.jobs import finish_job, start_job
from finrag.core.logging import LOG_FILE_NAME, bind, configure_logging, get_logger, reset_logging
from finrag.core.types import JobStatus


def _log_lines() -> list[str]:
    reset_logging()
    return (get_settings().paths.logs / LOG_FILE_NAME).read_text(encoding="utf-8").splitlines()


def test_json_log_lines_carry_bound_context() -> None:
    configure_logging()
    log = get_logger("ingestion")
    with bind(run_id="RUN-1", isin="INE000A01010"):
        log.info("fetched", extra={"event": "fetch_done", "fields": {"count": 3}})
    log.info("outside")
    first, second = (json.loads(line) for line in _log_lines())
    assert first["module"] == "ingestion"
    assert first["event"] == "fetch_done"
    assert first["run_id"] == "RUN-1" and first["isin"] == "INE000A01010"
    assert first["count"] == 3 and first["level"] == "INFO"
    assert second["event"] == "outside" and second["run_id"] is None


def test_configure_logging_is_idempotent() -> None:
    configure_logging()
    configure_logging()
    get_logger("core").info("once")
    assert len(_log_lines()) == 1


def _job_row(job_id: str) -> sa.Row:
    with transaction() as conn:
        return conn.execute(sa.select(tables.jobs).where(tables.jobs.c.job_id == job_id)).one()


def test_job_row_has_timestamps_and_stats() -> None:
    init_db()
    job_id = start_job("ingest", isin="INE000A01010")
    row = _job_row(job_id)
    assert row.status == JobStatus.RUNNING and row.started_at and row.finished_at is None
    finish_job(job_id, JobStatus.SUCCEEDED, {"fetched": 4, "unmapped": 1})
    row = _job_row(job_id)
    assert row.status == "succeeded" and row.finished_at and row.error is None
    assert json.loads(row.stats_json) == {"fetched": 4, "unmapped": 1}
    assert row.job_type == "ingest" and row.isin == "INE000A01010"


def test_finish_unknown_job_raises() -> None:
    init_db()
    with pytest.raises(KeyError):
        finish_job("JOB-missing", JobStatus.FAILED)
