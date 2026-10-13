"""`finrag normalize` and `finrag review` commands."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from finrag.core.config import ConfigError

normalize_app = typer.Typer(help="Map parsed facts to canonical concepts.", no_args_is_help=True)
review_app = typer.Typer(help="Work through the review queue.", no_args_is_help=True)


def _fail(message: object) -> typer.Exit:
    """Print an error and return the exit to raise."""
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=2)


@normalize_app.command("report")
def report(isin: Annotated[str, typer.Option(help="Company ISIN")]) -> None:
    """Show mapping coverage for an ISIN: % mapped by statement, known-unmapped by reason."""
    from finrag.core.db import init_db
    from finrag.normalize.report import build_report, format_report
    from finrag.normalize.runner import NormalizeError
    from finrag.normalize.taxonomy import TaxonomyError

    try:
        init_db()
        for line in format_report(build_report(isin)):
            typer.echo(line)
    except (ConfigError, TaxonomyError, NormalizeError) as exc:
        raise _fail(exc) from exc


@normalize_app.command("run")
def run(
    filing: Annotated[str | None, typer.Option(help="Filing ID; default: all parsed")] = None,
) -> None:
    """Normalize `parsed` filings and hand the candidates to the fact store (M05)."""
    from finrag.core.db import init_db
    from finrag.facts.derivation import derive_isin
    from finrag.facts.writer import WriteStats, write_candidates
    from finrag.normalize.runner import NormalizeError, run_normalize
    from finrag.normalize.taxonomy import TaxonomyError

    total = WriteStats()

    def writer(candidates):  # type: ignore[no-untyped-def]
        """Write candidates and remember the stats and ISINs for the derivation step."""
        stats = write_candidates(candidates)
        total.merge(stats)
        return stats

    try:
        init_db()
        reports = run_normalize(writer, filing)
        derived = WriteStats()
        for isin in sorted(total.isins):
            derived.merge(derive_isin(isin))
    except (ConfigError, TaxonomyError, NormalizeError) as exc:
        raise _fail(exc) from exc
    if total.fact_ids or derived.fact_ids or total.skipped:
        typer.echo(f"facts written: {json.dumps(total.as_dict(), sort_keys=True)}")
        typer.echo(f"facts derived: {json.dumps(derived.as_dict(), sort_keys=True)}")
    if not reports:
        typer.echo("nothing to normalize")
    failed = False
    for item in reports:
        typer.echo(f"{item.filing_id}: {item.status} {json.dumps(item.stats, sort_keys=True)}")
        if item.error:
            failed = True
            typer.echo(f"  error: {item.error}")
    if failed:
        raise typer.Exit(code=1)


@review_app.command("list")
def list_items(
    status: Annotated[str, typer.Option(help="open | resolved | ignored | all")] = "open",
    item_type: Annotated[str | None, typer.Option("--type", help="Item type")] = None,
) -> None:
    """List review items (default: open ones)."""
    from finrag.core.db import init_db
    from finrag.normalize.review_queue import list_items as fetch

    init_db()
    items = fetch(None if status == "all" else status, item_type)
    if not items:
        typer.echo("no review items")
    for item in items:
        payload = json.loads(item.payload_json)
        summary = (
            payload.get("key")
            or payload.get("file_name")
            or payload.get("summary")
            or payload.get("filing_id")
            or ""
        )
        extra = f"  suggestion: {payload['suggestion']}" if payload.get("suggestion") else ""
        typer.echo(
            f"{item.id:>5}  {item.status:<8} {item.item_type:<22} x{item.occurrences:<4} "
            f"{payload.get('isin', '')} {summary}{extra}"
        )


@review_app.command("resolve")
def resolve_item(
    item_id: Annotated[int, typer.Argument(help="Review item id")],
    concept: Annotated[str, typer.Option(help="Canonical concept from the taxonomy")],
) -> None:
    """Map an unmapped_concept item to a canonical concept; the next run applies it."""
    from finrag.core.db import init_db
    from finrag.normalize.review_queue import ReviewError, resolve
    from finrag.normalize.taxonomy import TaxonomyError, load_taxonomy

    try:
        init_db()
        resolve(item_id, concept, load_taxonomy())
    except (ConfigError, TaxonomyError, ReviewError) as exc:
        raise _fail(exc) from exc
    typer.echo(f"item {item_id} resolved to {concept}")


@review_app.command("ignore")
def ignore_item(item_id: Annotated[int, typer.Argument(help="Review item id")]) -> None:
    """Ignore a review item (an ignored unmapped_concept is not queued again)."""
    from finrag.core.db import init_db
    from finrag.normalize.review_queue import ReviewError, ignore

    try:
        init_db()
        ignore(item_id)
    except ReviewError as exc:
        raise _fail(exc) from exc
    typer.echo(f"item {item_id} ignored")
