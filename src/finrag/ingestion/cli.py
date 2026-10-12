"""`finrag ingest` commands: run, inbox, list, release, ignore, set-filed-on."""

from __future__ import annotations

from typing import Annotated

import sqlalchemy as sa
import typer

from finrag.core.config import ConfigError

ingest_app = typer.Typer(help="Fetch filings into the raw store.", no_args_is_help=True)


def _fail(message: object) -> typer.Exit:
    """Print an error and return the exit to raise."""
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=2)


def _echo_result(result) -> bool:
    """Print a RunResult; return True if anything failed."""
    failed = False
    for isin, report in result.reports.items():
        counts = report.stats()
        typer.echo(f"{isin}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        for error in report.errors:
            typer.echo(f"  error: {error}")
        failed |= bool(report.failed)
    for item in result.rejected:
        typer.echo(f"  needs review: {item.file_name}: {item.reason}")
    return failed


@ingest_app.command("run")
def run(
    isin: Annotated[
        str | None, typer.Option(help="ISIN or NSE symbol; default: watch list")
    ] = None,
    backfill: Annotated[bool, typer.Option(help="List history.years_back years")] = False,
) -> None:
    """Ingest from every enabled source for the watch list (or one company)."""
    from finrag.core.db import init_db
    from finrag.ingestion.registry import DbEntityLookup
    from finrag.ingestion.runner import run_ingest

    try:
        init_db()
        chosen = None
        if isin:
            found = DbEntityLookup().isin_for(isin)
            if found is None:
                raise _fail(f"'{isin}' is not a tracked company (run `finrag watchlist sync`)")
            chosen = [found]
        result = run_ingest(chosen, "backfill" if backfill else "incremental")
    except ConfigError as exc:
        raise _fail(exc) from exc
    if _echo_result(result):
        raise typer.Exit(code=1)


@ingest_app.command("inbox")
def inbox() -> None:
    """Ingest everything in the inbox folder."""
    from finrag.core.db import init_db
    from finrag.ingestion.runner import run_inbox

    try:
        init_db()
        result = run_inbox()
    except ConfigError as exc:
        raise _fail(exc) from exc
    if not result.reports and not result.rejected:
        typer.echo("inbox is empty")
    if _echo_result(result):
        raise typer.Exit(code=1)


@ingest_app.command("list")
def list_filings(isin: Annotated[str, typer.Option(help="ISIN or NSE symbol")]) -> None:
    """Show the registered filings for one company."""
    from finrag.core.db import init_db, tables, transaction
    from finrag.ingestion.registry import DbEntityLookup

    init_db()
    found = DbEntityLookup().isin_for(isin)
    if found is None:
        raise _fail(f"'{isin}' is not a tracked company")
    f = tables.filings
    with transaction() as conn:
        rows = conn.execute(
            sa.select(f)
            .where(f.c.isin == found)
            .order_by(f.c.period_end, f.c.filing_type, f.c.basis, f.c.revision_no)
        ).all()
    if not rows:
        typer.echo("no filings registered")
    for r in rows:
        typer.echo(
            f"{r.filing_id}  {r.filing_type:<24} {r.period_type or '-':<3} {r.period_end or '-'} "
            f"{r.basis:<12} {r.submission:<8} rev{r.revision_no} {r.format:<4} {r.status:<11} "
            f"{r.filed_on or '-'}"
        )


def _change(filing_id: str, action: str, note: str) -> None:
    """Release or ignore a quarantined filing."""
    from finrag.core.db import init_db
    from finrag.ingestion import registry

    init_db()
    try:
        getattr(registry, action)(filing_id, note)
    except registry.RegistryError as exc:
        raise _fail(exc) from exc
    typer.echo(f"{filing_id}: {action}d" if action == "release" else f"{filing_id}: ignored")


@ingest_app.command("release")
def release(filing_id: str, note: Annotated[str, typer.Option()] = "") -> None:
    """Release a quarantined filing so later modules may use it."""
    _change(filing_id, "release", note)


@ingest_app.command("ignore")
def ignore(filing_id: str, note: Annotated[str, typer.Option()] = "") -> None:
    """Keep a filing quarantined and mark its review item ignored."""
    _change(filing_id, "ignore", note)


@ingest_app.command("set-filed-on")
def set_filed_on(
    filing_id: str,
    filed_on: Annotated[str, typer.Argument(help="YYYY-MM-DD, or a full ISO date-time")],
    reason: Annotated[str, typer.Option(help="Why the stored date is wrong (required)")],
) -> None:
    """Correct a filing's filed_on; the old value, new value and reason are recorded."""
    from finrag.core.db import init_db
    from finrag.ingestion import registry

    init_db()
    try:
        old, new = registry.set_filed_on(filing_id, filed_on, reason)
    except registry.RegistryError as exc:
        raise _fail(exc) from exc
    typer.echo(f"{filing_id}: filed_on {old or '-'} -> {new} ({reason})")
