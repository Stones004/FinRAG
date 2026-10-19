"""`finrag validate` commands, and `finrag review confirm-event`."""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any

import typer

from finrag.core.config import ConfigError
from finrag.normalize.cli import review_app

validate_app = typer.Typer(help="Check the consistency of stored facts.", no_args_is_help=True)


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


def describe(status: str, details: dict[str, Any]) -> str:
    """One line saying why a result has its status."""
    if status == "skip":
        return "missing: " + ", ".join(details.get("missing") or [details.get("reason", "?")])
    if "differences" in details:
        beyond = sum(1 for d in details["differences"] if d["beyond_tolerance"])
        kinds = sorted({c["classification"] for c in details.get("clusters", [])})
        text = (
            f"{details['compared']} keys compared, {len(details['differences'])} differ beyond "
            f"rounding, {beyond} beyond tolerance"
        )
        return text + (f"; {', '.join(kinds)}" if kinds else "")
    if "abs_diff" in details:
        left = details.get("left", details.get("sum"))
        right = details.get("right", details.get("total"))
        return f"{left} vs {right}, gap {details['abs_diff']}"
    if "ratio" in details:
        return f"ratio {details['ratio']} (allowed {details['min']}..{details['max']})"
    return f"value {details.get('value')} ({details.get('expected')})"


@validate_app.command("run")
def run(isin: Annotated[str, typer.Option(help="ISIN or NSE symbol")]) -> None:
    """Run every rule on a company's facts; append the results; queue event proposals."""
    from finrag.validation.engine import run_validation
    from finrag.validation.rules import RulesError

    try:
        report = run_validation(_isin(isin))
    except (ConfigError, RulesError) as exc:
        raise _fail(exc) from exc
    typer.echo(f"run {report.run_id}: {json.dumps(report.counts())}")
    for (end, kind, basis), concepts in sorted(report.blocked().items()):
        typer.echo(f"BLOCKED {basis} {kind} {end}: {json.dumps(concepts, sort_keys=True)}")
    for row in report.results:
        if row.status.value == "warn":
            typer.echo(
                f"warn  {row.basis} {row.period_type} {row.period_end} {row.rule_id}: "
                f"{describe('warn', row.details)}"
            )
    for item in report.proposals:
        typer.echo(f"review item {item}: proposed corporate event (see `finrag review list`)")


@validate_app.command("show")
def show(
    isin: Annotated[str, typer.Option(help="ISIN or NSE symbol")],
    period: Annotated[str | None, typer.Option(help='e.g. "FY25", "Q4 FY26"')] = None,
    status: Annotated[str | None, typer.Option(help="pass | warn | block | skip")] = None,
) -> None:
    """Show the latest result of each rule (optionally for one period or status)."""
    from finrag.core.fiscal import label_to_period
    from finrag.validation.engine import latest_results

    try:
        wanted = label_to_period(period) if period else None
        rows = latest_results(_isin(isin))
    except (ConfigError, ValueError) as exc:
        raise _fail(exc) from exc
    shown = 0
    for row in rows:
        if status and row.status != status:
            continue
        if wanted and not (
            (row.period_end, row.period_type) == (wanted.end.isoformat(), wanted.type.value)
            or (
                row.period_type == "INSTANT"
                and row.period_end
                in (wanted.end.isoformat(), (wanted.start - date.resolution).isoformat())
            )
        ):
            continue
        shown += 1
        typer.echo(
            f"{row.basis:<12} {row.period_type:<7} {row.period_end}  {row.rule_id:<32} "
            f"{row.status:<5} {describe(row.status, json.loads(row.details_json))}"
        )
    if not shown:
        typer.echo("no results")


@review_app.command("confirm-event")
def confirm_event(
    item_id: Annotated[int, typer.Argument(help="corporate_event_proposal item id")],
    event_type: Annotated[
        str | None, typer.Option(help="accounting_policy_change | comparative_restatement")
    ] = None,
    event_date: Annotated[str | None, typer.Option(help="Effective date, YYYY-MM-DD")] = None,
    citation: Annotated[
        str | None, typer.Option(help="Where the company says so (note, page)")
    ] = None,
) -> None:
    """Confirm a proposed corporate event: records it and resolves the review item."""
    from finrag.core.db import init_db
    from finrag.normalize.review_queue import ReviewError
    from finrag.validation.events import confirm_event as confirm

    try:
        init_db()
        when = date.fromisoformat(event_date) if event_date else None
        event_id = confirm(item_id, event_type, when, citation)
    except (ReviewError, ValueError) as exc:
        raise _fail(exc) from exc
    typer.echo(f"item {item_id} resolved: corporate event {event_id} recorded")


@review_app.command("link-event")
def link_event(
    item_id: Annotated[int, typer.Argument(help="corporate_event_proposal item id")],
    event_id: Annotated[int, typer.Option("--event", help="Recorded corporate event id")],
) -> None:
    """Close a proposal as the same restatement as a recorded event (resolves the item)."""
    from finrag.core.db import init_db
    from finrag.normalize.review_queue import ReviewError
    from finrag.validation.events import link_item

    try:
        init_db()
        link_item(item_id, event_id)
    except ReviewError as exc:
        raise _fail(exc) from exc
    typer.echo(f"item {item_id} resolved: linked to corporate event {event_id}")
