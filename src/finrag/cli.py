"""Typer command-line entry point: `finrag`."""

from __future__ import annotations

import json

import typer

from finrag.core.config import ConfigError, Settings, get_settings, masked_settings
from finrag.core.db import init_db
from finrag.core.logging import configure_logging, get_logger
from finrag.facts.cli import facts_app
from finrag.ingestion.cli import ingest_app
from finrag.metrics.cli import metrics_app
from finrag.narrative.cli import narrative_app
from finrag.normalize.cli import normalize_app, review_app
from finrag.parsing.cli import parse_app
from finrag.query.cli import ask_command, routing_app
from finrag.validation.cli import validate_app
from finrag.watchlist.cli import watchlist_app

app = typer.Typer(help="FinRAG command line.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(watchlist_app, name="watchlist")
app.add_typer(ingest_app, name="ingest")
app.add_typer(parse_app, name="parse")
app.add_typer(normalize_app, name="normalize")
app.add_typer(review_app, name="review")
app.add_typer(facts_app, name="facts")
app.add_typer(validate_app, name="validate")
app.add_typer(metrics_app, name="metrics")
app.add_typer(narrative_app, name="narrative")
app.add_typer(routing_app, name="routing")
app.command("ask")(ask_command)


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
