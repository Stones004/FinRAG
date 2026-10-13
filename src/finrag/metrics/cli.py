"""`finrag metrics` commands: run, show, list."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

import typer

from finrag.core.config import ConfigError, get_settings
from finrag.core.types import MetricStatus, Period, PeriodType

metrics_app = typer.Typer(help="Compute and show derived metrics.", no_args_is_help=True)


def _fail(message: object) -> typer.Exit:
    """Print an error and return the exit to raise."""
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=2)


def _isin(identifier: str) -> str:
    """ISIN or symbol -> ISIN of a tracked company (exits with an error otherwise)."""
    from finrag.core.db import init_db
    from finrag.ingestion.registry import DbEntityLookup

    init_db()
    found = DbEntityLookup().isin_for(identifier)
    if found is None:
        raise _fail(f"'{identifier}' is not a tracked company")
    return found


def _period(text: str) -> Period:
    """'FY26' | 'Q4 FY26' | '2026-03-31' (a balance-sheet date) -> Period."""
    from finrag.core.fiscal import label_to_period

    try:
        day = date.fromisoformat(text)
    except ValueError:
        return label_to_period(text)
    return Period(day, day, PeriodType.INSTANT)


def _label(period: Period) -> str:
    from finrag.core.fiscal import period_to_label

    if period.type is PeriodType.INSTANT:
        return period.end.isoformat()
    try:
        return period_to_label(period)
    except ValueError:
        return f"{period.start}..{period.end}"


def format_value(value: Decimal | None, display: str) -> str:
    """A stored value -> text in its display unit (percent, crore, days, times, number)."""
    if value is None:
        return "-"
    settings = get_settings()
    places = Decimal(1).scaleb(-settings.defaults.display_decimals)
    if display == "percent":
        return f"{(value * 100).quantize(places, ROUND_HALF_UP):,}%"
    if display == "percentage_points":
        return f"{(value * 100).quantize(places, ROUND_HALF_UP):+,} pp"
    if display == "crore":
        unit = settings.defaults.display_unit
        scaled = value / Decimal(settings.units.multipliers[unit])
        return f"{scaled.quantize(places, ROUND_HALF_UP):,} {unit}"
    if display == "days":
        return f"{value.quantize(places, ROUND_HALF_UP):,} days"
    if display == "times":
        return f"{value.quantize(places, ROUND_HALF_UP):,}x"
    return f"{value.quantize(places, ROUND_HALF_UP):,}"


@metrics_app.command("run")
def run(
    isin: Annotated[str, typer.Option(help="ISIN or NSE symbol")],
    basis: Annotated[str | None, typer.Option(help="consolidated | standalone")] = None,
) -> None:
    """Compute every metric for a company; rows are written only where something changed."""
    from finrag.metrics.engine import run_metrics
    from finrag.metrics.registry import MetricsError

    try:
        report = run_metrics(_isin(isin), basis=basis)
    except (ConfigError, MetricsError, KeyError) as exc:
        raise _fail(exc) from exc
    typer.echo(
        f"run {report.run_id}: {report.computed} computed, "
        f"{sum(report.not_computed.values())} not computed; "
        f"{report.written} written, {report.unchanged} unchanged"
    )
    for code, count in report.not_computed.items():
        typer.echo(f"  {code}: {count}")


@metrics_app.command("show")
def show(
    isin: Annotated[str, typer.Option(help="ISIN or NSE symbol")],
    metric: Annotated[str, typer.Option(help="Metric ID, e.g. operating_margin")],
    period: Annotated[str | None, typer.Option(help='e.g. "FY26", "Q4 FY26", "2026-03-31"')] = None,
    basis: Annotated[str | None, typer.Option(help="consolidated | standalone")] = None,
) -> None:
    """Show the stored values of a metric with the flags that must accompany them."""
    from finrag.metrics.query import latest_values
    from finrag.metrics.registry import MetricsError, load_metrics

    try:
        registry = load_metrics()
        if metric not in registry:
            raise _fail(f"unknown metric '{metric}' (see `finrag metrics list`)")
        display = registry.get(metric).display
        wanted = _period(period) if period else None
        values = latest_values(_isin(isin), metric, basis)
    except (ConfigError, MetricsError, ValueError) as exc:
        raise _fail(exc) from exc
    shown = 0
    for v in values:
        if wanted and v.period != wanted:
            continue
        shown += 1
        text = format_value(v.value, display) if v.status is MetricStatus.COMPUTED else v.code
        flags = [f"basis_change:{e['event_id']}" for e in v.basis_change]
        if v.details.get("basis_fallback"):
            flags.append(f"basis_fallback:{v.details['basis_fallback']}")
        if v.details.get("assumed_zero"):
            flags.append(
                "assumed_zero:" + ",".join(z["concept"] for z in v.details["assumed_zero"])
            )
        if v.warnings:
            flags.append(f"warnings:{len(v.warnings)}")
        if v.details.get("restatements"):
            flags.append("restated_input")
        typer.echo(
            f"{v.basis:<12} {v.period.type.value:<7} {_label(v.period):<10} {text:<22} "
            f"v{v.formula_version}  {' '.join(flags)}"
        )
        typer.echo(f"{'':<12} {v.reason}")
    if not shown:
        typer.echo("no values (run `finrag metrics run` first)")


@metrics_app.command("list")
def list_metrics() -> None:
    """List the registry: ID, name, periods and sectors."""
    from finrag.metrics.registry import MetricsError, load_metrics

    try:
        registry = load_metrics()
    except (ConfigError, MetricsError) as exc:
        raise _fail(exc) from exc
    for metric_id in registry.order():
        m = registry.get(metric_id)
        periods = "/".join(p.value for p in m.period_types)
        sectors = ",".join(s.value for s in m.sectors) if m.sectors else "all"
        tag = (" (helper)" if m.helper else "") + (
            f" [prefers {m.preferred_basis}]" if m.preferred_basis else ""
        )
        typer.echo(f"{metric_id:<30} v{m.version:<4} {periods:<10} {sectors:<8} {m.name}{tag}")
