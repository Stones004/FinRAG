"""`finrag parse` commands: run, show."""

from __future__ import annotations

from typing import Annotated

import sqlalchemy as sa
import typer

from finrag.core.config import ConfigError

parse_app = typer.Typer(
    help="Parse fetched filings into facts and page text.", no_args_is_help=True
)

DEFAULT_SHOW_LIMIT = 20


def _fail(message: object) -> typer.Exit:
    """Print an error and return the exit to raise."""
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=2)


@parse_app.command("run")
def run(
    filing: Annotated[str | None, typer.Option(help="Filing ID; default: all fetched")] = None,
) -> None:
    """Parse all `fetched` filings (or one named filing)."""
    from finrag.core.db import init_db
    from finrag.parsing.runner import ParseError, run_parse

    try:
        init_db()
        reports = run_parse(filing)
    except (ConfigError, ParseError) as exc:
        raise _fail(exc) from exc
    if not reports:
        typer.echo("nothing to parse")
    failed = False
    for report in reports:
        counts = ", ".join(f"{k} {v}" for k, v in report.stats.items() if not isinstance(v, list))
        typer.echo(f"{report.filing_id}: {report.status}" + (f" ({counts})" if counts else ""))
        for key, value in report.stats.items():
            if isinstance(value, list) and value:
                typer.echo(f"  {key}: {value}")
        if report.error:
            failed = True
            typer.echo(f"  error: {report.error}")
    if failed:
        raise typer.Exit(code=1)


@parse_app.command("show")
def show(
    filing: Annotated[str, typer.Option(help="Filing ID")],
    limit: Annotated[int, typer.Option(help="How many facts to print")] = DEFAULT_SHOW_LIMIT,
) -> None:
    """Print the first parsed facts of a filing."""
    from finrag.core.db import init_db, tables, transaction

    init_db()
    facts = tables.parsed_facts
    with transaction() as conn:
        total = conn.execute(sa.select(sa.func.count()).where(facts.c.filing_id == filing)).scalar()
        rows = conn.execute(
            sa.select(facts).where(facts.c.filing_id == filing).order_by(facts.c.id).limit(limit)
        ).all()
    typer.echo(f"{total} parsed facts for {filing} (showing {len(rows)})")
    for r in rows:
        name = r.concept_raw or r.label_raw or "-"
        period = f"{r.period_start or '?'}..{r.period_end or '?'}"
        dims = " dims" if r.dimensions_json else ""
        typer.echo(
            f"{r.id:>6}  {name[:60]:<60} {period} {r.value if r.value is not None else 'nil'} "
            f"{r.unit_raw or '-'} x{r.scale if r.scale is not None else '?'} "
            f"{r.extractor} conf {r.confidence:.2f} {r.source_loc}{dims}"
        )
