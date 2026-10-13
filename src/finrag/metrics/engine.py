"""Compute metrics for one company from the M05 current view and write `metric_values`.

For each basis, each metric in dependency order and each period with facts, the engine applies the
rules of docs/modules/M07_metrics.md: sector and period applicability, input resolution (offsets,
instants, fallbacks), validation blocks, missing inputs, corporate-event breaks, evaluation, the
`basis_change` flag and the validation warnings. Nothing is guessed: an input that cannot be read
makes the value `not_computed` with the reason. Rows are appended only when something changed.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import get_settings
from finrag.core.db import tables, transaction
from finrag.core.fiscal import period_to_label, shift_period
from finrag.core.ids import PREFIX_METRIC, PREFIX_RUN, new_id
from finrag.core.jobs import finish_job, start_job
from finrag.core.logging import get_logger
from finrag.core.types import (
    EventType,
    JobStatus,
    MetricStatus,
    Period,
    PeriodType,
    decimal_to_text,
    utc_now_iso,
)
from finrag.facts.current_view import current_of, load_facts
from finrag.facts.dependencies import AffectedSet
from finrag.facts.models import Fact
from finrag.metrics import models as codes
from finrag.metrics.evaluator import EvalError, evaluate
from finrag.metrics.models import MetricRunReport
from finrag.metrics.registry import InputSpec, Metric, MetricRegistry, load_metrics
from finrag.normalize.taxonomy import Taxonomy, load_taxonomy
from finrag.validation.engine import is_blocked, warnings_for
from finrag.validation.events import CONFIRMABLE, recorded_events
from finrag.validation.rules import Rules, load_rules

log = get_logger("metrics")
Slot = tuple[date, date]
_BREAK_EVENTS = (EventType.MERGER.value, EventType.DEMERGER.value)
_FALLBACK_BASIS = "standalone"
_REQUESTED_FOR_FALLBACK = "consolidated"
_DURATIONS = (PeriodType.Q, PeriodType.H, PeriodType.NINE_M, PeriodType.FY)


# --- what a value rests on ----------------------------------------------------------------------


@dataclass
class _Acc:
    """Facts, dates, warnings and assumptions behind a value; merged up through metric inputs."""

    fact_ids: set[str] = field(default_factory=set)
    ends: set[date] = field(default_factory=set)
    warnings: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    assumed_zero: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    fallbacks: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    placeholder_zero: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    restatements: dict[int, dict[str, Any]] = field(default_factory=dict)
    standalone_used: bool = False

    def merge(self, other: _Acc) -> None:
        self.fact_ids |= other.fact_ids
        self.ends |= other.ends
        self.warnings.update(other.warnings)
        self.assumed_zero.update(other.assumed_zero)
        self.fallbacks.update(other.fallbacks)
        self.placeholder_zero.update(other.placeholder_zero)
        self.restatements.update(other.restatements)
        self.standalone_used = self.standalone_used or other.standalone_used


@dataclass
class _Res:
    """One resolved input."""

    value: Decimal | None = None
    acc: _Acc = field(default_factory=_Acc)
    missing: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    upstream_code: str | None = None  # why an input metric could not be computed
    present: bool = False  # something was found for it (a fact or an input metric's result)
    anchor: bool = False  # present, and read at the metric's own period (no offset)


@dataclass
class _Out:
    """The outcome of one metric at one period."""

    status: MetricStatus
    value: Decimal | None
    code: str
    reason: str
    details: dict[str, Any]
    fact_ids: list[str]
    acc: _Acc
    blocked: list[dict[str, Any]] = field(default_factory=list)


class _Facts:
    """The current fact per (basis, concept, slot) of one company, plus every version by ID."""

    def __init__(self, versions: list[Fact]) -> None:
        self.by_id = {f.fact_id: f for f in versions}
        grouped: dict[tuple[str, str], list[Fact]] = {}
        for fact in versions:
            grouped.setdefault((fact.basis, fact.concept), []).append(fact)
        self.current: dict[str, dict[str, dict[Slot, Fact]]] = {}
        for (basis, concept), group in grouped.items():
            self.current.setdefault(basis, {})[concept] = current_of(group)

    def get(self, basis: str, concept: str, slot: Slot) -> Fact | None:
        return self.current.get(basis, {}).get(concept, {}).get(slot)

    def bases(self) -> list[str]:
        return sorted(self.current)


def _shift_date(day: date, years: int) -> date:
    """A date moved by whole years (29 February becomes 28 February)."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def _shift(period: Period, spec: InputSpec) -> Period:
    """A metric period moved by an input's offset (same type)."""
    if spec.offset is None:
        return period
    if period.type is PeriodType.INSTANT:
        moved = _shift_date(period.end, spec.offset.years)
        return Period(moved, moved, PeriodType.INSTANT)
    return shift_period(period, years=spec.offset.years, periods=spec.offset.periods)


def _label(period: Period) -> str:
    """Period -> 'FY26' / 'Q4 FY26', or the date for an instant."""
    if period.type is PeriodType.INSTANT:
        return period.end.isoformat()
    try:
        return period_to_label(period)
    except ValueError:
        return f"{period.start}..{period.end}"


def _straddles(ends: Iterable[date], event_date: date) -> bool:
    """True if some period ends before the event date and another ends on or after it."""
    found = list(ends)
    return any(e < event_date for e in found) and any(e >= event_date for e in found)


@dataclass(frozen=True)
class _Event:
    """A recorded corporate event, decoded."""

    event_id: int
    event_type: str
    event_date: date
    basis: str | None
    citation: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_date": self.event_date.isoformat(),
            "citation": self.citation,
        }


# --- the per-basis calculation -----------------------------------------------------------------


class _Calculation:
    """Everything needed to compute the metrics of one company on one requested basis."""

    def __init__(
        self,
        isin: str,
        basis: str,
        sector: str,
        registry: MetricRegistry,
        taxonomy: Taxonomy,
        facts: _Facts,
        events: list[_Event],
        restated: dict[str, list[sa.Row]],
        rules: Rules,
        engine: Engine | None,
    ) -> None:
        self.isin, self.basis, self.sector = isin, basis, sector
        self.registry, self.taxonomy, self.facts = registry, taxonomy, facts
        self.events, self.restated, self.rules, self.engine = events, restated, rules, engine
        self.results: dict[tuple[str, date, date, PeriodType], _Out] = {}
        self._blocked_memo: dict[tuple[Any, ...], tuple[bool, list[str]]] = {}
        self._warning_memo: dict[tuple[Any, ...], list[dict[str, Any]]] = {}

    # -- periods -------------------------------------------------------------------------------

    def universe(self) -> tuple[list[Period], list[Period]]:
        """(duration periods, balance-sheet instants) that have facts on this basis."""
        durations: set[Period] = set()
        instants: set[date] = set()
        for concept, slots in self.facts.current.get(self.basis, {}).items():
            on_balance_sheet = self.taxonomy.concepts[concept].statement == "balance_sheet"
            for fact in slots.values():
                if fact.period.type in _DURATIONS:
                    durations.add(fact.period)
                elif on_balance_sheet:
                    instants.add(fact.period.end)
        ordered = sorted(durations, key=lambda p: (p.end, p.start))
        return ordered, [Period(d, d, PeriodType.INSTANT) for d in sorted(instants)]

    # -- validation lookups (memoised for the run) ----------------------------------------------

    def _is_blocked(
        self, concept: str, period: Period, basis: str, include_opening: bool
    ) -> tuple[bool, list[str]]:
        key = (concept, period, basis, include_opening)
        if key not in self._blocked_memo:
            self._blocked_memo[key] = is_blocked(
                self.isin,
                concept,
                period,
                basis,
                self.rules,
                self.engine,
                include_opening,
                self.taxonomy,
            )
        return self._blocked_memo[key]

    def _warnings(
        self, concept: str, period: Period, basis: str, include_opening: bool
    ) -> list[dict[str, Any]]:
        key = (concept, period, basis, include_opening)
        if key not in self._warning_memo:
            self._warning_memo[key] = warnings_for(
                self.isin, concept, period, basis, self.engine, include_opening, self.taxonomy
            )
        return self._warning_memo[key]

    # -- inputs --------------------------------------------------------------------------------

    def _slot(
        self, metric: Metric, spec: InputSpec, period: Period, source_instant: bool
    ) -> tuple[Period, Slot]:
        """Metric period and input -> (shifted period, the slot the input is read at)."""
        shifted = _shift(period, spec)
        if metric.is_instant:
            return shifted, (shifted.end, shifted.end)
        if source_instant:
            day = shifted.end if spec.at == "period_end" else shifted.start - timedelta(days=1)
            return shifted, (day, day)
        return shifted, (shifted.start, shifted.end)

    def _usable(
        self, concept: str, slot: Slot, spec: InputSpec, basis: str, res: _Res, alias: str
    ) -> Fact | None:
        """The current fact at a slot, unless it is a placeholder zero (see InputSpec)."""
        fact = self.facts.get(basis, concept, slot)
        guard = spec.zero_is_placeholder_while_nonzero
        if fact is None or guard is None:
            return fact
        why = None
        if fact.is_derived:
            why = "derived from a placeholder"
        elif fact.value == 0:
            other = self.facts.get(basis, guard, slot)
            if other is not None and other.value != 0:
                why = f"zero while {guard} is not zero"
        if why is None:
            return fact
        res.acc.placeholder_zero[(alias, slot[1].isoformat())] = {
            "alias": alias,
            "concept": concept,
            "period_end": slot[1].isoformat(),
            "why": why,
        }
        return None

    def _concept_input(self, metric: Metric, alias: str, spec: InputSpec, period: Period) -> _Res:
        assert spec.concept is not None
        concept = self.taxonomy.concepts[spec.concept]
        res = _Res()
        shifted, slot = self._slot(metric, spec, period, concept.period_kind == "instant")
        used_basis, used = self.basis, spec.concept
        fact = self._usable(spec.concept, slot, spec, self.basis, res, alias)
        if fact is None and spec.fallback_basis and self.basis == _REQUESTED_FOR_FALLBACK:
            fact = self._usable(spec.concept, slot, spec, spec.fallback_basis, res, alias)
            if fact is not None:
                used_basis = spec.fallback_basis
                res.acc.standalone_used = True
        if fact is None and spec.fallback and self._fallback_allowed(spec, slot):
            fact = self.facts.get(self.basis, spec.fallback.concept, slot)
            if fact is not None:
                used = spec.fallback.concept
                res.acc.fallbacks[(alias, slot[1].isoformat())] = {
                    "alias": alias,
                    "wanted": spec.concept,
                    "used": used,
                    "period_end": slot[1].isoformat(),
                    "why": "not reported; "
                    + ", ".join(spec.fallback.only_if_zero_or_absent)
                    + " is zero or absent at the period end",
                }
        if fact is None:
            if spec.missing == "zero":
                res.value = Decimal(0)
                res.acc.assumed_zero[(alias, slot[1].isoformat())] = {
                    "alias": alias,
                    "concept": spec.concept,
                    "period_end": slot[1].isoformat(),
                }
            else:
                # a rejected placeholder is something the company reported: say why it is unusable
                rejected = any(k[0] == alias for k in res.acc.placeholder_zero) or bool(
                    spec.fallback and self.facts.get(self.basis, spec.fallback.concept, slot)
                )
                res.present, res.anchor = rejected, rejected and spec.offset is None
                res.missing.append(
                    {"alias": alias, "concept": spec.concept, "period_end": slot[1].isoformat(),
                     "period": _label(shifted)}
                )  # fmt: skip
            return res
        res.value, res.present, res.anchor = fact.value, True, spec.offset is None
        res.acc.fact_ids.add(fact.fact_id)
        res.acc.ends.add(fact.period.end)
        self._attach(res, fact, used, shifted, slot, used_basis, metric, spec)
        return res

    def _fallback_allowed(self, spec: InputSpec, slot: Slot) -> bool:
        """Fallback conditions: each named instant concept is zero or absent at the period end."""
        assert spec.fallback is not None
        for name in spec.fallback.only_if_zero_or_absent:
            other = self.facts.get(self.basis, name, (slot[1], slot[1]))
            if other is not None and other.value != 0:
                return False
        return True

    def _attach(
        self,
        res: _Res,
        fact: Fact,
        concept: str,
        shifted: Period,
        slot: Slot,
        basis: str,
        metric: Metric,
        spec: InputSpec,
    ) -> None:
        """Blocks, warnings and flagged restatements of a fact that is used."""
        instant = slot[0] == slot[1]
        opening = bool(
            metric.include_opening
            and instant
            and not metric.is_instant
            and spec.offset is None
            and spec.at == "period_end"
        )
        where = Period(slot[0], slot[0], PeriodType.INSTANT) if instant and not opening else shifted
        blocked, rule_ids = self._is_blocked(concept, where, basis, opening)
        if blocked:
            res.blocked.append(
                {"concept": concept, "period_end": slot[1].isoformat(), "rule_ids": rule_ids}
            )
        for w in self._warnings(concept, where, basis, opening):
            res.acc.warnings[(w["rule_id"], w["period_end"], concept)] = {
                "rule_id": w["rule_id"],
                "period_end": w["period_end"],
                "concept": concept,
            }
        for row in self.restated.get(fact.fact_id, []):
            old = self.facts.by_id.get(row.old_fact_id)
            res.acc.restatements[row.id] = {
                "restatement_id": row.id,
                "concept": row.concept,
                "period_end": row.period_end,
                "old_fact_id": row.old_fact_id,
                "new_fact_id": row.new_fact_id,
                "old_value": decimal_to_text(old.value) if old else None,
                "new_value": decimal_to_text(fact.value),
                "abs_diff": row.abs_diff,
                "rel_diff": row.rel_diff,
            }

    def _metric_input(self, metric: Metric, alias: str, spec: InputSpec, period: Period) -> _Res:
        assert spec.metric is not None
        source = self.registry.get(spec.metric)
        shifted, slot = self._slot(metric, spec, period, source.is_instant)
        target = Period(slot[0], slot[1], PeriodType.INSTANT) if source.is_instant else shifted
        found = self.results.get((spec.metric, target.start, target.end, target.type))
        res = _Res()
        if found is None:
            res.missing.append(
                {"alias": alias, "metric": spec.metric, "period_end": target.end.isoformat(),
                 "period": _label(target)}
            )  # fmt: skip
            return res
        res.present, res.anchor = True, spec.offset is None
        if found.status is MetricStatus.COMPUTED:
            res.value = found.value
            res.acc.merge(found.acc)
        elif found.code == codes.BLOCKED:
            res.blocked.extend(found.blocked)
        else:
            res.upstream_code = found.code
            res.missing.append(
                {"alias": alias, "metric": spec.metric, "period_end": target.end.isoformat(),
                 "period": _label(target), "because": found.code}
            )  # fmt: skip
        return res

    # -- one value -----------------------------------------------------------------------------

    def compute(self, metric: Metric, period: Period) -> _Out | None:
        """One metric at one period -> its outcome, or None when the company has nothing for it."""
        if not metric.applies_to(self.sector):
            allowed = ", ".join(s.value for s in metric.sectors or ())
            return self._not_computed(
                codes.NOT_APPLICABLE,
                f"Not applicable to {self.sector} companies; defined for {allowed}.",
            )
        if period.type not in metric.period_types:
            defined = ", ".join(p.value for p in metric.period_types)
            return self._not_computed(
                codes.NOT_APPLICABLE,
                f"Not defined for {period.type.value} periods; defined for {defined}.",
            )
        values: dict[str, Decimal] = {}
        acc, blocked, missing = _Acc(), [], []
        anchored = False
        for alias, spec in metric.inputs.items():
            if spec.constant is not None:
                values[alias] = Decimal(str(spec.constant))
                continue
            res = (
                self._concept_input(metric, alias, spec, period)
                if spec.concept is not None
                else self._metric_input(metric, alias, spec, period)
            )
            anchored = anchored or res.anchor
            acc.merge(res.acc)
            blocked.extend(res.blocked)
            missing.extend(res.missing)
            if res.value is not None:
                values[alias] = res.value
        if not anchored:
            return None
        if blocked:
            names = sorted({r for b in blocked for r in b["rule_ids"]})
            where = ", ".join(sorted({f"{b['concept']} at {b['period_end']}" for b in blocked}))
            return self._not_computed(
                codes.BLOCKED,
                f"Blocked by validation ({', '.join(names)}) on {where}.",
                {"blocked_by": blocked},
                blocked=blocked,
            )
        if missing:
            text = "; ".join(
                f"{m['alias']} ({m.get('concept') or m['metric']}, {m['period']})" for m in missing
            )
            extra: dict[str, Any] = {"missing": missing}
            note = ""
            if acc.placeholder_zero:
                extra["placeholder_zero"] = [
                    acc.placeholder_zero[k] for k in sorted(acc.placeholder_zero)
                ]
                note = " A stored value was rejected as a placeholder."
            return self._not_computed(codes.MISSING_INPUT, f"Missing input: {text}.{note}", extra)
        return self._finish(metric, values, acc)

    def _finish(self, metric: Metric, values: dict[str, Decimal], acc: _Acc) -> _Out:
        """All inputs are present: corporate-event break, evaluation, flags."""
        for event in self.events:
            if event.event_type in _BREAK_EVENTS and _straddles(acc.ends, event.event_date):
                return self._not_computed(
                    codes.EVENT_BREAK,
                    f"A {event.event_type} on {event.event_date} (event {event.event_id}) falls "
                    "between the input periods.",
                    {"events": [event.as_dict()]},
                )
        try:
            value = evaluate(metric.formula, values)
        except EvalError as exc:
            code = codes.DIVISION_BY_ZERO if exc.code == codes.DIVISION_BY_ZERO else exc.code
            return self._not_computed(code, f"Cannot evaluate: {exc}.")
        crossed = [
            e
            for e in self.events
            if e.event_type in CONFIRMABLE
            and (e.basis is None or e.basis == self.basis)
            and _straddles(acc.ends, e.event_date)
        ]
        details = self._details(codes.COMPUTED, acc, crossed)
        return _Out(
            MetricStatus.COMPUTED,
            value,
            codes.COMPUTED,
            self._sentence(acc, crossed),
            details,
            sorted(acc.fact_ids),
            acc,
        )

    def _details(self, code: str, acc: _Acc, crossed: list[_Event]) -> dict[str, Any]:
        details: dict[str, Any] = {
            "code": code,
            "basis_requested": self.basis,
            "basis_used": _FALLBACK_BASIS if acc.standalone_used else self.basis,
        }
        if acc.standalone_used:
            details["basis_fallback"] = _FALLBACK_BASIS
        if crossed:
            details["basis_change"] = [e.as_dict() for e in crossed]
        for key, items in (
            ("assumed_zero", acc.assumed_zero),
            ("fallbacks", acc.fallbacks),
            ("placeholder_zero", acc.placeholder_zero),
            ("warnings", acc.warnings),
        ):
            if items:
                details[key] = [items[k] for k in sorted(items)]
        if acc.restatements:
            details["restatements"] = [acc.restatements[k] for k in sorted(acc.restatements)]
        return details

    def _sentence(self, acc: _Acc, crossed: list[_Event]) -> str:
        """A short human-readable account of the value; the exact flags are in `details`."""
        parts = ["Computed from reported facts."]
        for e in crossed:
            kind = e.event_type.replace("_", " ")
            parts.append(
                f"The input periods straddle the {kind} effective {e.event_date} (event "
                f"{e.event_id}); earlier periods are on the old basis."
            )
        if acc.standalone_used:
            parts.append("Uses the standalone figures: the consolidated filings report none.")
        if acc.fallbacks:
            used = sorted({f"{f['used']} instead of {f['wanted']}" for f in acc.fallbacks.values()})
            parts.append("Used " + ", ".join(used) + ".")
        if acc.placeholder_zero:
            parts.append("A stored zero was treated as not reported.")
        if acc.assumed_zero:
            names = sorted({z["concept"] for z in acc.assumed_zero.values()})
            parts.append("Assumed zero (not reported): " + ", ".join(names) + ".")
        if acc.restatements:
            names = sorted({r["concept"] for r in acc.restatements.values()})
            parts.append("An input was restated: " + ", ".join(names) + ".")
        if acc.warnings:
            parts.append(f"{len(acc.warnings)} validation warning(s) on the inputs.")
        return " ".join(parts)

    def _not_computed(
        self,
        code: str,
        reason: str,
        extra: dict[str, Any] | None = None,
        blocked: list[dict[str, Any]] | None = None,
    ) -> _Out:
        details = {"code": code, "basis_requested": self.basis, **(extra or {})}
        return _Out(
            MetricStatus.NOT_COMPUTED, None, code, reason, details, [], _Acc(), blocked or []
        )


# --- the run ------------------------------------------------------------------------------------


def _load_events(isin: str, engine: Engine | None) -> list[_Event]:
    """Recorded events that matter to metrics: policy changes, restatements, mergers, demergers."""
    rows = recorded_events(isin, (*CONFIRMABLE, *_BREAK_EVENTS), engine)
    events = []
    for row in rows:
        details = json.loads(row.details) if row.details else {}
        events.append(
            _Event(
                row.id,
                row.event_type,
                date.fromisoformat(row.event_date),
                details.get("basis"),
                details.get("citation"),
            )
        )
    return events


def _flagged_restatements(isin: str, engine: Engine | None) -> dict[str, list[sa.Row]]:
    """New fact ID -> its flagged restatement rows."""
    r = tables.restatements
    found: dict[str, list[sa.Row]] = {}
    with transaction(engine) as conn:
        for row in conn.execute(sa.select(r).where(r.c.isin == isin, r.c.flagged == 1)):
            found.setdefault(row.new_fact_id, []).append(row)
    return found


def _wanted(
    periods: Collection[Period] | None, affected: AffectedSet | None, isin: str
) -> tuple[set[tuple[str, date, date, PeriodType]] | None, set[Period] | None]:
    """Run filters -> (affected keys with dependants, plain periods); None means no filter."""
    keys: set[tuple[str, date, date, PeriodType]] | None = None
    if affected is not None:
        keys = set()
        for k in affected.periods:
            if k.isin != isin:
                continue
            base = Period(k.period_start, k.period_end, k.period_type)
            related = [base]
            if base.type is PeriodType.INSTANT:
                moved = _shift_date(base.end, 1)
                related.append(Period(moved, moved, PeriodType.INSTANT))
            else:
                try:
                    related.append(shift_period(base, years=1))
                except ValueError:
                    pass
                for day in (base.end, _shift_date(base.end, 1)):
                    related.append(Period(day, day, PeriodType.INSTANT))
            keys |= {(k.basis, p.start, p.end, p.type) for p in related}
    return keys, set(periods) if periods is not None else None


def _latest_rows(isin: str, engine: Engine | None) -> dict[tuple[str, ...], tuple[Any, ...]]:
    """Newest stored row per (metric, version, period, basis) -> what must differ to write again."""
    m = tables.metric_values
    latest: dict[tuple[str, ...], tuple[Any, ...]] = {}
    with transaction(engine) as conn:
        for row in conn.execute(sa.select(m).where(m.c.isin == isin).order_by(m.c.metric_value_id)):
            key = (row.metric_id, row.formula_version, row.period_start, row.period_end,
                   row.period_type, row.basis)  # fmt: skip
            latest[key] = (
                row.status,
                row.value,
                row.reason,
                row.input_fact_ids_json,
                row.details_json,
            )
    return latest


def run_metrics(
    isin: str,
    basis: str | None = None,
    periods: Collection[Period] | None = None,
    affected: AffectedSet | None = None,
    registry: MetricRegistry | None = None,
    engine: Engine | None = None,
) -> MetricRunReport:
    """Compute every metric of a company on each basis with facts; append rows that changed.

    `basis` limits the run to one basis; `periods` or an M05 `AffectedSet` limit the rows written to
    those periods (an affected set also covers the periods whose offsets read them).
    """
    registry = registry or load_metrics()
    taxonomy, rules = load_taxonomy(), load_rules()
    run_id = new_id(PREFIX_RUN)
    report = MetricRunReport(run_id, isin)
    job = start_job("metrics_run", isin, engine)
    try:
        with transaction(engine) as conn:
            sector = conn.execute(
                sa.select(tables.entities.c.sector_type).where(tables.entities.c.isin == isin)
            ).scalar()
            if (
                sector is None
                and conn.execute(
                    sa.select(tables.entities.c.isin).where(tables.entities.c.isin == isin)
                ).first()
                is None
            ):
                raise KeyError(f"unknown company {isin}")
            facts = _Facts(load_facts(conn, isin))
        sector = sector or get_settings().watchlist.default_sector_type
        events, restated = _load_events(isin, engine), _flagged_restatements(isin, engine)
        keys, plain = _wanted(periods, affected, isin)
        previous = _latest_rows(isin, engine)
        rows: list[dict[str, Any]] = []
        for requested in facts.bases():
            if basis is not None and requested != basis:
                continue
            calc = _Calculation(
                isin, requested, sector, registry, taxonomy, facts, events, restated, rules, engine
            )
            durations, instants = calc.universe()
            for metric_id in registry.order():
                metric = registry.get(metric_id)
                for period in instants if metric.is_instant else durations:
                    out = calc.compute(metric, period)
                    if out is None:
                        continue
                    calc.results[(metric_id, period.start, period.end, period.type)] = out
                    if (keys is not None and (requested, *_span(period)) not in keys) or (
                        plain is not None and period not in plain
                    ):
                        continue
                    rows.append(_row(isin, requested, metric, period, out, run_id))
        _write(rows, previous, report, engine)
        finish_job(job, JobStatus.SUCCEEDED, _stats(report), engine=engine)
    except Exception as exc:
        finish_job(job, JobStatus.FAILED, error=str(exc), engine=engine)
        raise
    log.info("metrics run", extra={"event": "metrics_run", "isin": isin, "run_id": run_id})
    return report


def _span(period: Period) -> tuple[date, date, PeriodType]:
    return period.start, period.end, period.type


def _row(
    isin: str, basis: str, metric: Metric, period: Period, out: _Out, run_id: str
) -> dict[str, Any]:
    """An outcome -> the metric_values row to insert."""
    return {
        "metric_value_id": new_id(PREFIX_METRIC),
        "isin": isin,
        "metric_id": metric.id,
        "formula_version": metric.version,
        "period_start": period.start.isoformat(),
        "period_end": period.end.isoformat(),
        "period_type": period.type.value,
        "basis": basis,
        "value": decimal_to_text(out.value) if out.value is not None else None,
        "status": out.status.value,
        "reason": out.reason,
        "input_fact_ids_json": json.dumps(out.fact_ids),
        "details_json": json.dumps(out.details, sort_keys=True, default=str),
        "run_id": run_id,
        "created_at": utc_now_iso(),
    }


def _write(
    rows: list[dict[str, Any]],
    previous: dict[tuple[str, ...], tuple[Any, ...]],
    report: MetricRunReport,
    engine: Engine | None,
) -> None:
    """Insert the rows whose result differs from the newest stored row of the same key."""
    fresh = []
    for row in rows:
        key = (row["metric_id"], row["formula_version"], row["period_start"], row["period_end"],
               row["period_type"], row["basis"])  # fmt: skip
        content = (row["status"], row["value"], row["reason"], row["input_fact_ids_json"],
                   row["details_json"])  # fmt: skip
        report.by_code[json.loads(row["details_json"])["code"]] += 1
        if previous.get(key) == content:
            report.unchanged += 1
        else:
            fresh.append(row)
    if fresh:
        with transaction(engine) as conn:
            conn.execute(sa.insert(tables.metric_values), fresh)
    report.written = len(fresh)


def _stats(report: MetricRunReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "written": report.written,
        "unchanged": report.unchanged,
        "by_code": dict(report.by_code),
    }
