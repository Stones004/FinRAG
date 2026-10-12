"""Typer command-line entry point: `finrag`."""

from __future__ import annotations

import json

import typer

from finrag.core.config import ConfigError, Settings, get_settings, masked_settings
from finrag.core.db import init_db
from finrag.core.logging import configure_logging, get_logger
from finrag.watchlist.cli import watchlist_app

app = typer.Typer(help="FinRAG command line.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(watchlist_app, name="watchlist")


def _load_settings() -> Settings:
    """Return settings, turning ConfigError into a clean CLI failure."""
    try:
        return get_settings()
    except ConfigError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def init() -> None:
    """Create any missing data folders and the database (existing files are never touched)."""
    settings = _load_settings()
    created = []
    for folder in settings.paths.data_folders():
        if not folder.exists():
            folder.mkdir(parents=True)
            created.append(folder)
    init_db()
    configure_logging(settings)
    get_logger("core").info("init complete", extra={"event": "init"})
    for folder in created:
        typer.echo(f"created {folder}")
    typer.echo(f"database ready at {settings.paths.db}")


@config_app.command("show")
def config_show() -> None:
    """Print the resolved settings as JSON, secrets masked."""
    typer.echo(json.dumps(masked_settings(_load_settings()), indent=2))


@app.command()
def status() -> None:
    """Per-company health (placeholder; filled in by later modules)."""
    _load_settings()
    typer.echo("No status available yet: later modules add company health reporting.")
