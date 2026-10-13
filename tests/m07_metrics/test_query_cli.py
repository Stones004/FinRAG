"""Reading stored values (version policy, series) and the `finrag metrics` commands."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from typer.testing import CliRunner

from finrag.cli import app
from finrag.core import config
from finrag.core.types import PeriodType
from finrag.metrics.cli import format_value
from finrag.metrics.engine import run_metrics
from finrag.metrics.query import get_metric, get_metric_series, latest_values

D = Decimal
FY26, FY25 = ("2025-04-01", "2026-03-31", "FY"), ("2024-04-01", "2025-03-31", "FY")
runner = CliRunner()


def _pin(root: Path, metric: str, version: str) -> None:
    path = root / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    data["metrics"]["version_policy"] = "pinned"
    data["metrics"]["pinned_versions"] = {metric: version}
    path.write_text(yaml.safe_dump(data), "utf-8")
    config.reset_settings_cache()


def _bump(kit) -> None:
    def change(data):
        data["metrics"]["net_margin"]["version"] = "1.1"
        data["metrics"]["net_margin"]["formula"] = "pat / rev * 100"

    kit.edit_registry(change)


def test_get_metric_returns_none_before_any_run(kit) -> None:
    kit.load_acme()
    assert get_metric(kit.ISIN, "roe", kit.period(*FY26), "consolidated") is None


def test_latest_policy_reads_the_newest_version(kit) -> None:
    kit.load_acme()
    run_metrics(kit.ISIN)
    _bump(kit)
    run_metrics(kit.ISIN)
    v = get_metric(kit.ISIN, "net_margin", kit.period(*FY26), "consolidated")
    assert v.formula_version == "1.1" and v.value == D(110) / D(1000) * 100


def test_pinned_policy_reads_the_pinned_version(kit, isolated_repo) -> None:
    kit.load_acme()
    run_metrics(kit.ISIN)
    _bump(kit)
    run_metrics(kit.ISIN)
    _pin(isolated_repo, "net_margin", "1.0")
    v = get_metric(kit.ISIN, "net_margin", kit.period(*FY26), "consolidated")
    assert v.formula_version == "1.0" and v.value == D(110) / D(1000)
    other = get_metric(kit.ISIN, "operating_margin", kit.period(*FY26), "consolidated")
    assert other is not None  # an unpinned metric still follows `latest`


def test_series_has_one_value_per_period_oldest_first(kit) -> None:
    kit.load_acme()
    run_metrics(kit.ISIN)
    _bump(kit)
    run_metrics(kit.ISIN)
    series = get_metric_series(kit.ISIN, "net_margin", "consolidated", PeriodType.FY, 5)
    assert [v.period.end.isoformat() for v in series] == ["2025-03-31", "2026-03-31"]
    assert [v.formula_version for v in series] == ["1.1", "1.1"]
    assert len(get_metric_series(kit.ISIN, "net_margin", "consolidated", PeriodType.FY, 1)) == 1
    assert get_metric_series(kit.ISIN, "net_margin", "consolidated", PeriodType.FY, 0) == []
    assert len(latest_values(kit.ISIN, "net_margin", "consolidated")) == 2


def test_requested_basis_is_part_of_the_key(kit) -> None:
    kit.load_acme()
    run_metrics(kit.ISIN)
    assert get_metric(kit.ISIN, "roe", kit.period(*FY26), "standalone") is None


# --- display --------------------------------------------------------------------------------------


def test_format_value() -> None:
    assert format_value(D("0.0723892972"), "percent") == "7.24%"
    assert format_value(D("-0.0035039"), "percentage_points") == "-0.35 pp"
    assert format_value(D("33783600000"), "crore") == "3,378.36 crore"
    assert format_value(D("52.909492"), "days") == "52.91 days"
    assert format_value(D("1.6635768"), "times") == "1.66x"
    assert format_value(None, "percent") == "-"


# --- CLI ------------------------------------------------------------------------------------------


def test_cli_list_run_show(kit) -> None:
    kit.load_acme()
    listed = runner.invoke(app, ["metrics", "list"])
    assert listed.exit_code == 0, listed.output
    assert "operating_margin" in listed.output and "(helper)" in listed.output

    ran = runner.invoke(app, ["metrics", "run", "--isin", kit.ISIN])
    assert ran.exit_code == 0, ran.output
    assert "computed" in ran.output and "not_applicable" in ran.output

    again = runner.invoke(app, ["metrics", "run", "--isin", kit.ISIN])
    assert " 0 written" in again.output

    shown = runner.invoke(
        app,
        ["metrics", "show", "--isin", kit.ISIN, "--metric", "operating_margin", "--period", "FY26"],
    )
    assert shown.exit_code == 0, shown.output
    assert "17.00%" in shown.output and "FY26" in shown.output

    capital = runner.invoke(
        app,
        [
            "metrics",
            "show",
            "--isin",
            kit.ISIN,
            "--metric",
            "capital_employed",
            "--period",
            "2026-03-31",
        ],
    )
    assert "assumed_zero:lease_liabilities" in capital.output
    assert "74.00 crore" in capital.output  # 740 million


def test_cli_rejects_unknown_metric_and_company(kit) -> None:
    kit.load_acme()
    bad_metric = runner.invoke(app, ["metrics", "show", "--isin", kit.ISIN, "--metric", "nope"])
    assert bad_metric.exit_code == 2 and "unknown metric" in bad_metric.output
    bad_company = runner.invoke(app, ["metrics", "run", "--isin", "NOSUCH"])
    assert bad_company.exit_code == 2 and "not a tracked company" in bad_company.output


def test_cli_show_without_a_run_says_so(kit) -> None:
    kit.load_acme()
    out = runner.invoke(app, ["metrics", "show", "--isin", kit.ISIN, "--metric", "roe"])
    assert out.exit_code == 0 and "no values" in out.output
