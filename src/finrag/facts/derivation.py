"""Derive discrete periods from cumulative ones.

Q2 = H1 - Q1, Q3 = 9M - H1, Q4 = FY - 9M, H2 = FY - H1.

Only for duration concepts the taxonomy allows (`Concept.can_derive`: not `derivable: false`, and
per-share or ratio concepts only if marked additive). Inputs must be reported (never derived)
current facts, XBRL or PDF/OCR at or above `parsing.pdf.min_table_confidence`. A target that is
reported directly is never derived. "Blocked" periods are not M05's concern: M06/M07 gate them.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.fiscal import (
    cumulative_steps,
    fiscal_year_of,
    fy_label,
    label_to_period,
    period_to_label,
)
from finrag.core.types import Period, SourceClass
from finrag.facts.current_view import current_of, load_facts
from finrag.facts.models import Fact
from finrag.facts.writer import NewFact, WriteStats, append_fact
from finrag.normalize.taxonomy import Concept, Taxonomy, load_taxonomy

TARGET_LABELS = ("Q2", "Q3", "Q4", "H2")

Group = tuple[str, str, str]  # (isin, concept, basis)


def eligible_input(fact: Fact, settings: Settings) -> bool:
    """XBRL facts, and PDF/OCR facts at or above the table confidence floor, may be inputs."""
    if fact.source_class is SourceClass.XBRL:
        return True
    floor = settings.parsing.pdf.min_table_confidence
    return fact.source_class is not SourceClass.DERIVED and (fact.confidence or 0.0) >= floor


def targets_in_year(year: int) -> list[Period]:
    """Fiscal year number -> its derivable discrete periods (Q2, Q3, Q4, H2)."""
    return [label_to_period(f"{t} {fy_label(year)}") for t in TARGET_LABELS]


def target_periods_fed_by(period: Period) -> list[Period]:
    """A period -> the derivable periods (Q2, Q3, Q4, H2) whose identities use it as an input."""
    found = []
    for target in targets_in_year(fiscal_year_of(period.end)):
        steps = cumulative_steps(target)
        if any(t.period == period for step in steps for t in step.terms):
            found.append(target)
    return found


def _build(target: Period, inputs: list[tuple[int, Fact]], concept: Concept) -> NewFact:
    """Target period + signed input facts -> the derived fact to append."""
    latest = max((f for _, f in inputs), key=lambda f: f.order)
    classes = sorted({f.source_class.value for _, f in inputs})
    formula = f"{period_to_label(target)} = " + " ".join(
        f"{'+' if sign > 0 else '-'} {period_to_label(f.period)}" for sign, f in inputs
    ).lstrip("+ ")
    return NewFact(
        isin=latest.isin,
        concept=latest.concept,
        period=target,
        basis=latest.basis,
        value=sum((sign * f.value for sign, f in inputs), Decimal(0)),
        unit=concept.unit,
        source_filing_id=latest.source_filing_id,
        parsed_fact_id=None,
        mapping_method=None,
        source_class=SourceClass.DERIVED,
        filed_on=latest.filed_on,
        derivation={
            "formula": formula,
            "input_fact_ids": [f.fact_id for _, f in inputs],
            "inputs": [
                {
                    "fact_id": f.fact_id,
                    "sign": sign,
                    "period": period_to_label(f.period),
                    "source_class": f.source_class.value,
                }
                for sign, f in inputs
            ],
            "input_classes": classes,
            "all_xbrl": classes == [SourceClass.XBRL.value],
        },
    )


def derivations_for(versions: list[Fact], concept: Concept, settings: Settings) -> list[NewFact]:
    """All stored versions of one (isin, concept, basis) -> the derived facts it supports."""
    reported = [f for f in versions if not f.is_derived]
    reported_keys = {(f.period.start, f.period.end) for f in reported}
    current = current_of(reported)
    years = {fiscal_year_of(f.period.end) for f in reported}
    out: list[NewFact] = []
    for year in sorted(years):
        for target in targets_in_year(year):
            if (target.start, target.end) in reported_keys:
                continue
            for step in cumulative_steps(target):
                inputs = [
                    (term.sign, current.get((term.period.start, term.period.end)))
                    for term in step.terms
                ]
                if all(f is not None and eligible_input(f, settings) for _, f in inputs):
                    out.append(_build(target, [(s, f) for s, f in inputs if f], concept))
                    break
    return out


def groups_of(isin: str, engine: Engine | None = None) -> list[Group]:
    """ISIN -> the (isin, concept, basis) groups that have reported facts."""
    c = tables.canonical_facts
    with transaction(engine) as conn:
        rows = conn.execute(
            sa.select(c.c.concept, c.c.basis)
            .where(c.c.isin == isin, c.c.is_derived == 0)
            .distinct()
            .order_by(c.c.concept, c.c.basis)
        ).all()
    return [(isin, r.concept, r.basis) for r in rows]


def derive_groups(
    groups: Iterable[Group],
    engine: Engine | None = None,
    settings: Settings | None = None,
    taxonomy: Taxonomy | None = None,
) -> WriteStats:
    """Derive what the inputs support for each (isin, concept, basis); append via the writer."""
    settings = settings or get_settings()
    taxonomy = taxonomy or load_taxonomy()
    stats = WriteStats()
    with transaction(engine) as conn:
        for isin, concept_name, basis in sorted(set(groups)):
            concept = taxonomy.concepts.get(concept_name)
            if concept is None or not concept.can_derive:
                continue
            versions = load_facts(conn, isin, [concept_name], basis)
            for new in derivations_for(versions, concept, settings):
                append_fact(conn, new, settings, stats)
    return stats


def derive_isin(
    isin: str,
    engine: Engine | None = None,
    settings: Settings | None = None,
    taxonomy: Taxonomy | None = None,
) -> WriteStats:
    """Derive every supported discrete period for one company."""
    return derive_groups(groups_of(isin, engine), engine, settings, taxonomy)
