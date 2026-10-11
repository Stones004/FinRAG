"""One test per M00 acceptance criterion (docs/modules/M00_foundation.md)."""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from typer.testing import CliRunner

from finrag.cli import app
from finrag.core.config import ConfigError, FiscalSettings, load_settings
from finrag.core.db import get_engine, init_db, tables, transaction
from finrag.core.fiscal import label_to_period, shift_period
from finrag.core.ids import filing_id_from_sha
from finrag.core.jobs import finish_job, start_job
from finrag.core.types import JobStatus, decimal_to_text, text_to_decimal, to_decimal

SRC = Path(__file__).resolve().parents[2] / "src"
SECTION_7_TABLES = {
    "entities", "entity_aliases", "corporate_events", "filings", "parsed_facts",
    "canonical_facts", "review_queue", "restatements", "validation_results", "metric_values",
    "chunks", "query_log", "jobs", "alerts", "signals",
}  # fmt: skip


def test_ac1_init_creates_folders_and_every_table(isolated_repo: Path) -> None:
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    for folder in ["raw", "processed", "indexes", "reference", "inbox", "logs"]:
        assert (isolated_repo / "data" / folder).is_dir()
    assert (isolated_repo / "data" / "finrag.db").is_file()
    assert SECTION_7_TABLES <= set(sa.inspect(get_engine()).get_table_names())


def test_ac2_invalid_settings_name_the_key(isolated_repo: Path) -> None:
    path = isolated_repo / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["fiscal"]["fy_start_month"] = 13
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"fiscal\.fy_start_month"):
        load_settings()


def test_ac3_only_config_module_touches_yaml() -> None:
    pattern = re.compile(r"\byaml\b|safe_load|ruamel|\.ya?ml\b", re.IGNORECASE)
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if p.name != "config.py" and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_ac4_money_is_never_float() -> None:
    with pytest.raises(TypeError):
        to_decimal(0.1)
    assert to_decimal("0.1") == Decimal("0.1")
    original = Decimal("123456789012345.678901234567")
    assert text_to_decimal(decimal_to_text(original)) == original


def test_ac5_fiscal_periods() -> None:
    april = FiscalSettings(fy_start_month=4, fy_label_format="FY{yy}")
    january = FiscalSettings(fy_start_month=1, fy_label_format="FY{yy}")
    q1 = label_to_period("Q1 FY26", april)
    assert (q1.start, q1.end) == (date(2025, 4, 1), date(2025, 6, 30))
    fy = label_to_period("FY26", april)
    assert (fy.start, fy.end) == (date(2025, 4, 1), date(2026, 3, 31))
    q3 = label_to_period("Q3 FY26", april)
    assert shift_period(q3, years=-1, fiscal=april) == label_to_period("Q3 FY25", april)
    q3j = label_to_period("Q3 FY26", january)
    assert (q3j.start, q3j.end) == (date(2026, 7, 1), date(2026, 9, 30))
    assert shift_period(q3j, years=-1, fiscal=january) == label_to_period("Q3 FY25", january)


def test_ac6_filing_id_is_deterministic() -> None:
    sha = "0123456789abcdef" * 4
    assert filing_id_from_sha(sha) == filing_id_from_sha(sha) == "FIL-0123456789ab"


def test_ac7_job_row_written_with_timestamps_and_stats() -> None:
    init_db()
    job_id = start_job("pipeline")
    finish_job(job_id, JobStatus.SUCCEEDED, {"parsed": 2})
    with transaction() as conn:
        row = conn.execute(sa.select(tables.jobs).where(tables.jobs.c.job_id == job_id)).one()
    assert row.started_at and row.finished_at and '"parsed": 2' in row.stats_json
