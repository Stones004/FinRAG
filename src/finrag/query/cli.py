"""`finrag ask` and `finrag routing test`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from finrag.core.config import ConfigError, get_settings
from finrag.core.db import init_db
from finrag.query.models import Answer, Clarification

routing_app = typer.Typer(help="Routing test of the question parser.", no_args_is_help=True)


def _utf8() -> None:
    """Print the typographic characters of an answer on consoles that default to a code page."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")


def _fail(message: object) -> typer.Exit:
    """Print an error and return the exit to raise."""
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=2)


def ask_command(
    question: Annotated[str, typer.Argument(help="The question, in your own words.")],
    explain_plan: Annotated[
        bool, typer.Option("--explain-plan", help="Print the intent and plan; no LLM call.")
    ] = False,
) -> None:
    """Answer a question about a tracked company, with citations."""
    from finrag.query.service import build_service

    _utf8()
    try:
        init_db()
        service = build_service(use_configured_llm=True)
        if explain_plan:
            result = service.explain_plan(question)
            if isinstance(result, Clarification):
                typer.echo(json.dumps(result.to_json(), indent=2, default=str))
            else:
                intent, plan = result
                typer.echo(
                    json.dumps(
                        {"intent": intent.to_json(), "plan": plan.to_json()}, indent=2, default=str
                    )
                )
            return
        answer = service.ask(question)
    except ConfigError as exc:
        raise _fail(exc) from exc
    if isinstance(answer, Clarification):
        typer.echo(answer.message)
        for candidate in answer.candidates:
            typer.echo(f"  - {candidate.id}: {candidate.name}")
        return
    assert isinstance(answer, Answer)
    typer.echo(answer.text)


@routing_app.command("test")
def routing_test(
    cases: Annotated[
        Path | None, typer.Option("--cases", help="Cases file (default: the routing set).")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="List every case.")] = False,
) -> None:
    """Run the routing cases through the parser (rules only): pass rates and wrong-confident."""
    from finrag.query.entity_resolver import load_entities
    from finrag.query.intent_parser import build_parser
    from finrag.query.routing import run_routing

    _utf8()
    try:
        init_db()
        parser = build_parser(llm=None)
        isins = {e.symbol: e.isin for e in load_entities()}
        report = run_routing(parser, isins, cases)
    except (ConfigError, FileNotFoundError) as exc:
        raise _fail(exc) from exc
    typer.echo(report.summary())
    for result in report.results if verbose else report.failures():
        mark = "ok  " if result.passed else "FAIL"
        typer.echo(f"{mark} {result.id} {result.question!r}")
        for problem in result.problems:
            typer.echo(f"       {problem}")
    target = get_settings().query.routing.min_accuracy
    low = report.rate(report.group(False)) < target or report.rate(report.group(True)) < target
    if report.wrong_confident or low:
        raise typer.Exit(code=1)
