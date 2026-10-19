"""`finrag narrative` commands: index, search, skipped, sections."""

from __future__ import annotations

import json
from typing import Annotated

import sqlalchemy as sa
import typer

from finrag.core.config import ConfigError

narrative_app = typer.Typer(
    help="Index and search transcripts, XBRL text blocks and results-PDF notes.",
    no_args_is_help=True,
)


def _fail(message: object) -> typer.Exit:
    """Print an error and return the exit to raise."""
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=2)


@narrative_app.command("index")
def index(
    isin: Annotated[str | None, typer.Option(help="Only this company")] = None,
    filing: Annotated[str | None, typer.Option(help="Only this filing ID")] = None,
    reindex: Annotated[
        bool, typer.Option(help="Re-chunk filings that already have chunks")
    ] = False,
    rebuild_vectors: Annotated[
        bool, typer.Option(help="Re-embed every chunk (after changing the embedding model)")
    ] = False,
) -> None:
    """Chunk parsed filings that have no chunks yet and refresh the BM25 and vector indexes."""
    from finrag.core.db import init_db
    from finrag.narrative.runner import run_index

    try:
        init_db()
        reports = run_index(isin, filing, reindex, rebuild_vectors)
    except ConfigError as exc:
        raise _fail(exc) from exc
    if not reports:
        typer.echo("nothing to index")
    failed = False
    for report in reports:
        line = f"{report.filing_id}  {report.isin}  {report.status}  chunks={report.chunks}"
        typer.echo(f"{line}  {report.detail}".rstrip())
        failed = failed or report.status == "failed"
    if failed:
        raise typer.Exit(code=1)


@narrative_app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(help="What to look for")],
    isin: Annotated[str, typer.Option(help="Company ISIN")],
    section: Annotated[list[str] | None, typer.Option(help="Section filter, repeatable")] = None,
    period: Annotated[
        list[str] | None, typer.Option(help="Exact period such as 'Q1 FY27', repeatable")
    ] = None,
    fy: Annotated[list[str] | None, typer.Option(help="Fiscal year such as FY26")] = None,
    source: Annotated[list[str] | None, typer.Option(help="transcript or results")] = None,
    k: Annotated[int | None, typer.Option("--k", "-k", help="Number of hits")] = None,
) -> None:
    """Search one company's narrative with filters (BM25 + embeddings, fused)."""
    from finrag.core.config import get_settings
    from finrag.core.fiscal import label_to_period
    from finrag.narrative.embedder import SentenceTransformerEmbedder
    from finrag.narrative.search import IndexOutOfDate, SearchRequest, search

    try:
        settings = get_settings()
        periods = [label_to_period(p) for p in period] if period else None
        request = SearchRequest(isin, query, section, periods, fy, source, k)
        hits = search(request, SentenceTransformerEmbedder(settings.narrative), settings)
    except (ConfigError, ValueError, IndexOutOfDate) as exc:
        raise _fail(exc) from exc
    if not hits:
        typer.echo("no matching chunks")
    for number, hit in enumerate(hits, start=1):
        where = f"p.{hit.page}" if hit.page is not None else (hit.concept or "text block")
        who = f" {hit.speaker} ({hit.speaker_role})" if hit.speaker else ""
        typer.echo(
            f"[{number}] {hit.chunk_id}  {hit.source_type}/{hit.section}  "
            f"{hit.period_label or hit.period_end}  {where}{who}  rrf={hit.scores['rrf']:.4f}"
        )
        typer.echo(f"    {hit.text[:300]}")


@narrative_app.command("skipped")
def skipped(
    filing: Annotated[str, typer.Option(help="Filing ID")],
) -> None:
    """Print the per-filing summary of pages kept and skipped (and why)."""
    from finrag.core.config import get_settings
    from finrag.core.db import tables, transaction
    from finrag.narrative.runner import SUMMARY_FILE, TYPE_ANNUAL

    settings = get_settings()
    with transaction() as conn:
        row = conn.execute(
            sa.select(tables.filings).where(tables.filings.c.filing_id == filing)
        ).first()
    if row is None:
        raise _fail(f"unknown filing: {filing}")
    if row.filing_type == TYPE_ANNUAL:
        typer.echo(f"{filing}: deferred (annual reports are not indexed in v1)")
        return
    path = settings.paths.processed / filing / SUMMARY_FILE
    if not path.exists():
        raise _fail(f"{filing} has no narrative summary yet: run `finrag narrative index`")
    typer.echo(
        json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=1, ensure_ascii=False)
    )


@narrative_app.command("sections")
def sections(
    isin: Annotated[str | None, typer.Option(help="Only this company")] = None,
    filing: Annotated[str | None, typer.Option(help="Only this filing ID")] = None,
) -> None:
    """Report, per results PDF: notes pages, auditor/review report pages and text quality.

    Read-only: recomputes the sections from the parsed page text (nothing is written).
    """
    from finrag.core.config import get_settings
    from finrag.core.db import tables, transaction
    from finrag.narrative.patterns import load_narrative_patterns
    from finrag.narrative.runner import analyse_results_pdf
    from finrag.narrative.sectioner import spans_of

    settings, patterns = get_settings(), load_narrative_patterns()
    f = tables.filings
    query = sa.select(f).where(f.c.filing_type == "financial_results", f.c.format == "pdf")
    if isin:
        query = query.where(f.c.isin == isin)
    if filing:
        query = query.where(f.c.filing_id == filing)
    with transaction() as conn:
        rows = list(conn.execute(query.order_by(f.c.isin, f.c.period_end, f.c.filing_id)))
    for row in rows:
        header = f"{row.filing_id}  {row.isin}  {row.period_end}  {row.basis}"
        try:
            slices, summary, _ = analyse_results_pdf(
                settings.paths.processed / row.filing_id, settings, patterns
            )
        except FileNotFoundError:
            typer.echo(f"{header}: not parsed (no page text)")
            continue
        typer.echo(f"{header}  pages={summary.pages_total} kept={summary.pages_kept}")
        for name in ("results_notes", "auditor_report"):
            spans = [s for s in spans_of(slices) if s.section == name]
            if not spans:
                typer.echo(f"    {name}: none found")
                continue
            for span in spans:
                qualities = [
                    s.quality
                    for s in slices
                    if s.section == name
                    and span.start_page <= s.page <= span.end_page
                    and s.quality is not None
                ]
                low = min(qualities) if qualities else float("nan")
                typer.echo(
                    f"    {name}: pages {span.start_page}-{span.end_page}  min quality {low:.2f}"
                )
        if summary.partial_pages:
            typer.echo(f"    partial pages (statement part dropped): {summary.partial_pages}")
        for item in summary.skipped:
            span = f"{item.start_page}-{item.end_page}"
            typer.echo(f"    skipped {span}  {item.reason}  {item.detail[:40]!r}")
