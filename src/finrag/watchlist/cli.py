"""`finrag watchlist` commands: sync, list, resolve."""

from __future__ import annotations

import sqlalchemy as sa
import typer

from finrag.core.config import ConfigError, get_settings

watchlist_app = typer.Typer(help="Manage the watch list.", no_args_is_help=True)


def _fail(exc: Exception) -> typer.Exit:
    """Print a ConfigError-style message and return the exit to raise."""
    typer.echo(f"Error: {exc}", err=True)
    return typer.Exit(code=2)


@watchlist_app.command("sync")
def sync() -> None:
    """Resolve the watch list file and update the entity registry."""
    from finrag.core.db import init_db
    from finrag.watchlist.registry import sync_watchlist

    try:
        init_db()
        report = sync_watchlist()
    except ConfigError as exc:
        raise _fail(exc) from exc
    typer.echo(
        f"added {len(report.added)}, updated {len(report.updated)}, "
        f"unchanged {len(report.unchanged)}, needs review {len(report.needs_review)}"
    )
    for label, isins in (
        ("added", report.added),
        ("paused", report.paused),
        ("reactivated", report.reactivated),
    ):
        for isin in isins:
            typer.echo(f"  {label}: {isin}")
    for item in report.needs_review:
        typer.echo(f"  needs review: '{item.identifier}' ({item.status}): {item.reason}")


@watchlist_app.command("list")
def list_entities() -> None:
    """Show the registered entities."""
    from finrag.core.db import init_db, tables, transaction

    init_db()
    with transaction() as conn:
        rows = conn.execute(sa.select(tables.entities).order_by(tables.entities.c.name)).all()
    if not rows:
        typer.echo("No entities yet. Run `finrag watchlist sync`.")
        return
    for r in rows:
        typer.echo(
            f"{r.isin}  {r.nse_symbol or '-':<12} {r.sector_type:<9} {r.default_basis:<12} "
            f"{r.status:<7} {r.name}"
        )


@watchlist_app.command("resolve")
def resolve_text(text: str) -> None:
    """Dry-run: show what an identifier would resolve to (nothing is written)."""
    from finrag.watchlist.master import load_master
    from finrag.watchlist.resolver import resolve

    try:
        resolution = resolve(text, load_master(), get_settings())
    except ConfigError as exc:
        raise _fail(exc) from exc
    typer.echo(f"{resolution.status}" + (f" via {resolution.method}" if resolution.method else ""))
    for record in resolution.candidates:
        typer.echo(f"  {record.isin}  {record.nse_symbol or '-':<12} {record.name}")
    if resolution.reason:
        typer.echo(f"  {resolution.reason}")
    if resolution.status != "resolved":
        raise typer.Exit(code=1)
