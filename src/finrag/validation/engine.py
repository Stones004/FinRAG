"""Run the rules for one company and store the results; answer "is this concept blocked?".

Results are append-only: every run writes new rows with a new run_id, and the latest row per
(isin, period_end, period_type, basis, rule_id) is the current result. Facts are read through the
M05 current view (XBRL first), so a check never sees two versions of a number; `cross_source` is the
one rule that reads every stored version.
"""

from __future__ import annotations

import dataclasses
import json
from collections import defaultdict
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from finrag.core.config import Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.ids import PREFIX_RUN, new_id
from finrag.core.jobs import finish_job, start_job
from finrag.core.logging import get_logger
from finrag.core.types import JobStatus, Period, PeriodType, ValidationStatus, utc_now_iso
from finrag.facts.basis_check import default_group, misbased_columns
from finrag.facts.current_view import current_of, load_facts
from finrag.facts.dependencies import PeriodKey
from finrag.facts.models import Fact
from finrag.normalize.taxonomy import Taxonomy, load_taxonomy
from finrag.validation import checks
from finrag.validation.cross_source import Cluster, analyse, explain
from finrag.validation.events import propose_disagreement, propose_event
from finrag.validation.restatement_notes import find_note
from finrag.validation.rules import (
    CrossPeriodRule,
    CrossSourceRule,
    EqualityRule,
    RangeRule,
    Rule,
    Rules,
    SignRule,
    SumRule,
    load_rules,
)

log = get_logger("validation")
Slot = tuple[date, date]  # (start, end); an instant has start == end
Current = dict[str, dict[Slot, Fact]]  # concept -> slot -> current fact


@dataclass(frozen=True)
class ResultRow:
    """One validation result: a rule at a period and basis."""

    isin: str
    period_end: str
    period_type: str
    basis: str
    rule_id: str
    status: ValidationStatus
    details: dict[str, Any]


@dataclass
class Evaluation:
    """Rows and restated clusters from one pass over a company's facts (nothing written)."""

    results: list[ResultRow] = field(default_factory=list)
    restated: list[tuple[str, Cluster, str]] = field(default_factory=list)  # (isin, cluster, rule)
    # native-text PDF comparatives that differ from the original with nothing to explain it
    disagreements: list[tuple[str, Cluster, str]] = field(default_factory=list)


@dataclass
class ValidationReport:
    """What a run found."""

    run_id: str
    isin: str
    results: list[ResultRow]
    proposals: list[int]

    def counts(self) -> dict[str, int]:
        """Status -> number of results."""
        found: dict[str, int] = defaultdict(int)
        for row in self.results:
            found[row.status.value] += 1
        return dict(sorted(found.items()))

    def blocked(self) -> dict[tuple[str, str, str], dict[str, list[str]]]:
        """(period_end, period_type, basis) -> blocked concept -> rule ids, from this run."""
        rules = load_rules()
        out: dict[tuple[str, str, str], dict[str, list[str]]] = {}
        for row in self.results:
            rule = rules.get(row.rule_id)
            if row.status is not ValidationStatus.BLOCK or rule is None:
                continue
            slot = out.setdefault((row.period_end, row.period_type, row.basis), {})
            for concept in rule.blocks_concepts:
                slot.setdefault(concept, []).append(row.rule_id)
        return out


# --- evaluation --------------------------------------------------------------------------------


def _current(versions: list[Fact]) -> Current:
    """Every stored version of one basis -> the current fact per concept and slot."""
    by_concept: dict[str, list[Fact]] = defaultdict(list)
    for fact in versions:
        by_concept[fact.concept].append(fact)
    return {c: current_of(v) for c, v in by_concept.items()}


def _slot_wanted(slot: Slot, basis: str, periods: Collection[PeriodKey] | None) -> bool:
    """True if no filter is given, or the slot is one of the periods (or an edge instant of one)."""
    if periods is None:
        return True
    for key in periods:
        if key.basis != basis:
            continue
        if slot == (key.period_start, key.period_end):
            return True
        if slot[0] == slot[1] and slot[0] in (key.period_end, key.period_start - timedelta(days=1)):
            return True
    return False


def _row(
    isin: str, basis: str, slot: Slot, fact: Fact, rule: Rule, result: checks.CheckResult
) -> ResultRow:
    """A check result at a slot -> ResultRow (period type from a fact of that slot)."""
    return ResultRow(
        isin,
        slot[1].isoformat(),
        fact.period.type.value,
        basis,
        rule.id,
        result.status,
        result.details,
    )


def _slots(rule: Rule, current: Current, needed: list[str], instant: bool) -> list[Slot]:
    """Slots at which any of `needed` has a fact, of the rule's period kind."""
    found: set[Slot] = set()
    for concept in needed:
        found.update(s for s in current.get(concept, {}) if (s[0] == s[1]) == instant)
    return sorted(found, key=lambda s: (s[1], s[0]))


def _simple_rows(
    isin: str,
    basis: str,
    rule: Rule,
    rules: Rules,
    current: Current,
    periods: Collection[PeriodKey] | None,
    settings: Settings,
) -> list[ResultRow]:
    """equality, cross_statement, sum, sign, range: one row per slot with any input."""
    instant = rules.kinds[rule.id] == "instant"
    needed = rule.concepts()
    if isinstance(rule, SumRule):
        needed = [*rule.parts, *rule.minus, rule.total]
    rows: list[ResultRow] = []
    for slot in _slots(rule, current, needed, instant):
        if not _slot_wanted(slot, basis, periods):
            continue
        facts = {c: current[c][slot] for c in rule.concepts() if slot in current.get(c, {})}
        if isinstance(rule, EqualityRule):
            result = checks.check_equality(rule, facts, settings.validation)
        elif isinstance(rule, SumRule):
            result = checks.check_sum(rule, facts, settings.validation)
        elif isinstance(rule, SignRule):
            result = checks.check_sign(rule, facts, settings.validation)
        else:
            assert isinstance(rule, RangeRule)
            result = checks.check_range(rule, facts, settings.validation)
        rows.append(_row(isin, basis, slot, next(iter(facts.values())), rule, result))
    return rows


def _cross_period_rows(
    isin: str,
    basis: str,
    rule: CrossPeriodRule,
    current: Current,
    periods: Collection[PeriodKey] | None,
    settings: Settings,
) -> list[ResultRow]:
    """Opening balance at a date vs the closing balance at that same date."""
    rows: list[ResultRow] = []
    for slot, opening in sorted(current.get(rule.opening, {}).items(), key=lambda kv: kv[0][1]):
        if not _slot_wanted(slot, basis, periods):
            continue
        closing = current.get(rule.closing, {}).get(slot)
        result = checks.check_cross_period(rule, opening, closing, settings.validation)
        rows.append(_row(isin, basis, slot, opening, rule, result))
    return rows


def _cross_source_rows(
    isin: str,
    basis: str,
    rule: CrossSourceRule,
    versions: list[Fact],
    periods: Collection[PeriodKey] | None,
    settings: Settings,
    notes: Callable[[str, str], dict[str, Any] | None],
) -> tuple[list[ResultRow], list[Cluster], list[Cluster]]:
    """XBRL vs PDF/OCR: one row per period compared; the clusters that are restatements, and the
    comparatives that disagree with an original without anything to explain it."""
    clusters = analyse(versions, list(rule.identities), settings)
    for cluster in clusters:
        if cluster.differences:
            explain(
                cluster,
                notes(cluster.pdf_filing_id, cluster.basis) if cluster.comparative else None,
                settings,
            )
    ends: dict[tuple[date, str], list[Cluster]] = defaultdict(list)
    for cluster in clusters:
        for slot in cluster.compared:
            ends[slot].append(cluster)
    rows: list[ResultRow] = []
    for (end, period_type), group in sorted(ends.items()):
        if periods is not None and not any(
            k.basis == basis and k.period_end == end for k in periods
        ):
            continue
        compared = sum(c.compared[(end, period_type)] for c in group)
        diffs = [
            d
            for c in group
            for d in c.differences
            if (d.period.end, d.period.type.value) == (end, period_type)
        ]
        breaches = [d for d in diffs if d.breach]
        details: dict[str, Any] = {
            "compared": compared,
            "differences": [d.as_dict() for d in diffs],
            "concepts": sorted({d.concept for d in breaches}),
        }
        if diffs:
            shown = [
                c
                for c in group
                if any(
                    (d.period.end, d.period.type.value) == (end, period_type) for d in c.differences
                )
            ]
            details["clusters"] = [c.summary() for c in shown]
        status = rule.fail_status if breaches else ValidationStatus.PASS
        rows.append(ResultRow(isin, end.isoformat(), period_type, basis, rule.id, status, details))
    restated = [c for c in clusters if c.explained_by]
    disagreeing = [c for c in clusters if c.comparative and c.differences and not c.explained_by]
    return rows, restated, disagreeing


def _sector_of(conn: Connection, isin: str) -> str:
    """ISIN -> its sector type (general when not set)."""
    value = conn.execute(
        sa.select(tables.entities.c.sector_type).where(tables.entities.c.isin == isin)
    ).scalar()
    return value or "general"


def evaluate_isin(
    isin: str,
    rules: Rules | None = None,
    settings: Settings | None = None,
    periods: Collection[PeriodKey] | None = None,
    engine: Engine | None = None,
) -> Evaluation:
    """Run every applicable rule on a company's facts -> rows and restated clusters (read-only)."""
    rules = rules or load_rules()
    settings = settings or get_settings()
    out = Evaluation()
    with transaction(engine) as conn:
        sector = _sector_of(conn, isin)
        versions = load_facts(conn, isin, include_rejected=True)
    by_basis: dict[str, list[Fact]] = defaultdict(list)
    for fact in versions:
        by_basis[fact.basis].append(fact)
    # A PDF column whose values match the other basis's XBRL on every shared key was filed under
    # the wrong basis (its page names none): compare it with the basis it belongs to.
    misbased = misbased_columns(by_basis, settings)
    found_notes: dict[tuple[str, str], dict[str, Any] | None] = {}

    def note_for(rule: CrossSourceRule) -> Callable[[str, str], dict[str, Any] | None]:
        """(Filing id, basis) -> the filing's restatement note (read once per run), or None."""

        def look(filing_id: str, basis: str) -> dict[str, Any] | None:
            if (filing_id, basis) not in found_notes:
                match = find_note(filing_id, rule.restatement_notes, basis, engine)
                found_notes[(filing_id, basis)] = None if match is None else match.as_dict()
            return found_notes[(filing_id, basis)]

        return look

    compared: dict[str, list[Fact]] = defaultdict(list)
    for basis, facts in by_basis.items():
        for fact in facts:
            true = misbased.get((default_group(fact), basis))
            if true and fact.source_class.value in ("pdf", "ocr"):
                compared[true].append(dataclasses.replace(fact, basis=true))
            else:
                compared[basis].append(fact)
    for basis, facts in sorted(by_basis.items()):
        current = _current([f for f in facts if not f.gap_rejected])
        for rule in rules.rules:
            if not rules.applies_to(rule, sector):
                continue
            if isinstance(rule, CrossSourceRule):
                rows, restated, disagreeing = _cross_source_rows(
                    isin, basis, rule, compared[basis], periods, settings, note_for(rule)
                )
                for row in rows:
                    moved = [
                        {"pdf_filing": f, "labelled": b, "values_match": t}
                        for ((f, e), b), t in misbased.items()
                        if t == basis and e.isoformat() == row.period_end
                    ]
                    if moved:
                        row.details["basis_corrected"] = moved
                out.restated.extend((isin, c, rule.id) for c in restated)
                out.disagreements.extend((isin, c, rule.id) for c in disagreeing)
            elif isinstance(rule, CrossPeriodRule):
                rows = _cross_period_rows(isin, basis, rule, current, periods, settings)
            else:
                rows = _simple_rows(isin, basis, rule, rules, current, periods, settings)
            out.results.extend(rows)
    return out


def run_validation(
    isin: str,
    periods: Collection[PeriodKey] | None = None,
    rules: Rules | None = None,
    engine: Engine | None = None,
) -> ValidationReport:
    """Evaluate, append the rows under a new run_id, queue event proposals -> ValidationReport."""
    job = start_job("validate", isin, engine)
    run_id = new_id(PREFIX_RUN)
    try:
        evaluation = evaluate_isin(isin, rules, None, periods, engine)
        now = utc_now_iso()
        proposals: list[int] = []
        with transaction(engine) as conn:
            for row in evaluation.results:
                conn.execute(
                    sa.insert(tables.validation_results).values(
                        isin=row.isin,
                        period_end=row.period_end,
                        period_type=row.period_type,
                        basis=row.basis,
                        rule_id=row.rule_id,
                        status=row.status.value,
                        details_json=json.dumps(row.details, sort_keys=True, default=str),
                        run_id=run_id,
                        created_at=now,
                    )
                )
            rounding = get_settings().validation.cross_source.rounding_inr
            for code, cluster, rule_id in evaluation.restated:
                item = propose_event(conn, code, cluster, rule_id, rounding)
                if item is not None:
                    proposals.append(item)
            for code, cluster, rule_id in evaluation.disagreements:
                propose_disagreement(conn, code, cluster, rule_id)
        report = ValidationReport(run_id, isin, evaluation.results, sorted(set(proposals)))
        finish_job(job, JobStatus.SUCCEEDED, {"run_id": run_id, **report.counts()}, engine=engine)
    except Exception as exc:
        finish_job(job, JobStatus.FAILED, error=str(exc), engine=engine)
        raise
    log.info("validation run", extra={"event": "validate", "isin": isin, "run_id": run_id})
    return report


# --- reading results ---------------------------------------------------------------------------


def _instants(period: Period, opening: bool, closing: bool) -> set[date]:
    """Dates whose instant results apply to a period.

    A balance at the end of a duration is `closing`, the balance it starts from (the day before
    its start) is `opening`. An INSTANT period is only itself.
    """
    if period.type is PeriodType.INSTANT:
        return {period.end}
    found: set[date] = set()
    if closing:
        found.add(period.end)
    if opening:
        found.add(period.start - timedelta(days=1))
    return found


def _scope(
    concept: str, include_opening: bool, taxonomy: Taxonomy | None = None
) -> tuple[bool, bool]:
    """Concept -> (looks at the opening date, looks at the closing date) of a duration.

    A plain concept is judged at the period end only. An opening balance (`opening_cash`) is
    judged at the day before the start only. `include_opening` (metrics that average opening and
    closing balances) adds the opening date to a plain concept.
    """
    if concept in (taxonomy or load_taxonomy()).opening_names:
        return True, False
    return include_opening, True


def _latest(
    conn: Connection, isin: str, basis: str, period: Period, scope: tuple[bool, bool]
) -> list[sa.Row]:
    """Latest result per (period_end, period_type, rule) that applies to the period."""
    vr = tables.validation_results
    instants = {d.isoformat() for d in _instants(period, *scope)}
    ends = instants | {period.end.isoformat()}
    rows = conn.execute(
        sa.select(vr)
        .where(vr.c.isin == isin, vr.c.basis == basis, vr.c.period_end.in_(sorted(ends)))
        .order_by(vr.c.id)
    ).all()
    latest = {(r.period_end, r.period_type, r.rule_id): r for r in rows}
    keep = []
    for (end, kind, _rule), row in latest.items():
        if kind == PeriodType.INSTANT.value and end in instants:
            keep.append(row)
        elif (end, kind) == (period.end.isoformat(), period.type.value):
            keep.append(row)
    return keep


def is_blocked(
    isin: str,
    concept: str,
    period: Period,
    basis: str,
    rules: Rules | None = None,
    engine: Engine | None = None,
    include_opening: bool = False,
    taxonomy: Taxonomy | None = None,
) -> tuple[bool, list[str]]:
    """(isin, concept, period, basis) -> (blocked, rule ids), from the latest results.

    A duration is blocked by instant results at its end date. An opening balance (`opening_cash`)
    is looked up at the day before the period starts instead. Pass `include_opening=True` for a
    metric that averages opening and closing balances, to also see blocks at the opening date.
    A caller that asks many times passes the loaded `taxonomy` to avoid re-reading it.
    """
    rules = rules or load_rules()
    with transaction(engine) as conn:
        rows = _latest(conn, isin, basis, period, _scope(concept, include_opening, taxonomy))
    reasons = []
    for row in rows:
        rule = rules.get(row.rule_id)
        if row.status == ValidationStatus.BLOCK.value and rule and concept in rule.blocks_concepts:
            reasons.append(row.rule_id)
    return bool(reasons), sorted(set(reasons))


def warnings_for(
    isin: str,
    concept: str,
    period: Period,
    basis: str,
    engine: Engine | None = None,
    include_opening: bool = False,
    taxonomy: Taxonomy | None = None,
) -> list[dict[str, Any]]:
    """Warn results that name this concept (to be shown wherever the number is shown)."""
    with transaction(engine) as conn:
        rows = _latest(conn, isin, basis, period, _scope(concept, include_opening, taxonomy))
    found = []
    for row in rows:
        details = json.loads(row.details_json)
        if row.status == ValidationStatus.WARN.value and concept in details.get("concepts", []):
            found.append({"rule_id": row.rule_id, "period_end": row.period_end, "details": details})
    return found


def latest_results(isin: str, engine: Engine | None = None) -> list[sa.Row]:
    """The current (latest) result of every rule, period and basis of a company."""
    vr = tables.validation_results
    with transaction(engine) as conn:
        rows = conn.execute(sa.select(vr).where(vr.c.isin == isin).order_by(vr.c.id)).all()
    latest = {(r.basis, r.period_end, r.period_type, r.rule_id): r for r in rows}
    return sorted(latest.values(), key=lambda r: (r.basis, r.period_end, r.period_type, r.rule_id))
