"""`finrag facts` commands: show, restatements, derive."""

from __future__ import annotations

from typing import Annotated

import sqlalchemy as sa
import typer

from finrag.core.config import ConfigError

facts_app = typer.Typer(help="Inspect and derive canonical facts.", no_args_is_help=True)


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


@facts_app.command("show")
def show(
    isin: Annotated[str, typer.Option(help="ISIN or NSE symbol")],
    concept: Annotated[str, typer.Option(help="Canonical concept, e.g. revenue")],
    basis: Annotated[str | None, typer.Option(help="consolidated | standalone")] = None,
) -> None:
    """Show the current series of a concept: value, period, source filing and derived flag."""
    from finrag.facts.current_view import get_facts

    try:
        found = get_facts(_isin(isin), [concept], None, basis)
    except ConfigError as exc:
        raise _fail(exc) from exc
    if not found:
        typer.echo("no facts")
    for f in found:
        flag = "derived" if f.is_derived else f.source_class.value
        typer.echo(
            f"{f.basis:<12} {f.period.type.value:<7} {f.period.start}..{f.period.end}  "
            f"{f.value:>22}  {f.unit}  {f.source_filing_id}  rev{f.revision_no}  {flag}  "
            f"{f.fact_id}"
        )


@facts_app.command("restatements")
def restatements(
    isin: Annotated[str, typer.Option(help="ISIN or NSE symbol")],
    flagged_only: Annotated[bool, typer.Option(help="Only flagged restatements")] = False,
) -> None:
    """List recorded restatements for a company (old -> new value, differences, flag)."""
    from finrag.core.db import tables, transaction

    code = _isin(isin)
    r, c = tables.restatements, tables.canonical_facts
    old, new = c.alias("old"), c.alias("new")
    query = (
        sa.select(r, old.c.value.label("old_value"), new.c.value.label("new_value"))
        .select_from(
            r.join(old, old.c.fact_id == r.c.old_fact_id).join(
                new, new.c.fact_id == r.c.new_fact_id
            )
        )
        .where(r.c.isin == code)
        .order_by(r.c.concept, r.c.period_end, r.c.id)
    )
    if flagged_only:
        query = query.where(r.c.flagged == 1)
    with transaction() as conn:
        rows = conn.execute(query).all()
    if not rows:
        typer.echo("no restatements")
    for x in rows:
        typer.echo(
            f"{'FLAGGED' if x.flagged else 'recorded':<8} {x.basis:<12} {x.concept:<28} "
            f"{x.period_start}..{x.period_end}  {x.old_value} -> {x.new_value}  "
            f"abs {x.abs_diff}  rel {x.rel_diff or '-'}"
        )


@facts_app.command("derive")
def derive(isin: Annotated[str, typer.Option(help="ISIN or NSE symbol")]) -> None:
    """Derive missing discrete periods (Q2, Q3, Q4, H2) from cumulative ones."""
    from finrag.facts.derivation import derive_isin

    try:
        stats = derive_isin(_isin(isin))
    except ConfigError as exc:
        raise _fail(exc) from exc
    typer.echo(", ".join(f"{k} {v}" for k, v in stats.as_dict().items()))
