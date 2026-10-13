"""Load and validate the `metrics` registry.

Every metric declares its inputs (taxonomy concepts, other metrics or constants), a formula in
the small language of `evaluator.py`, the period types and sectors it applies to, and the
assumptions and limitations that must be shown next to its numbers. The registry is data: a new
metric needs no code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from finrag.core.config import load_registry
from finrag.core.types import PeriodType, SectorType
from finrag.metrics.evaluator import Compiled, EvalError, compile_formula
from finrag.normalize.taxonomy import Taxonomy, load_taxonomy

_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_ALIAS = re.compile(r"^[a-z_][a-z0-9_]*$")
_DURATION_TYPES = (PeriodType.Q, PeriodType.H, PeriodType.NINE_M, PeriodType.FY)
DISPLAYS = ("percent", "percentage_points", "crore", "days", "times", "number")


class MetricsError(Exception):
    """Raised when the metric registry is invalid; the message names every problem found."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Offset(_Model):
    """Move the metric's period before reading the input."""

    years: int = 0
    periods: int = 0


class Fallback(_Model):
    """Use another concept when the primary one is absent, if the conditions hold."""

    concept: str
    # instant concepts (at the period end) that must be zero or absent for the fallback to apply
    only_if_zero_or_absent: list[str] = []


class InputSpec(_Model):
    """One input of a metric: exactly one of `concept`, `metric` or `constant`."""

    concept: str | None = None
    metric: str | None = None
    constant: int | float | None = None
    offset: Offset | None = None
    at: Literal["period_end", "period_start"] | None = None
    missing: Literal["fail", "zero"] = "fail"
    # the requested basis is consolidated and the fact is absent: read the standalone fact
    fallback_basis: Literal["standalone"] | None = None
    # a stored 0 is a placeholder (not a disclosure) while this concept is non-zero in the same
    # period; derived values of the concept are then not trusted either
    zero_is_placeholder_while_nonzero: str | None = None
    fallback: Fallback | None = None

    @model_validator(mode="after")
    def _one_source(self) -> InputSpec:
        given = [x for x in (self.concept, self.metric, self.constant) if x is not None]
        if len(given) != 1:
            raise ValueError("exactly one of concept, metric or constant is required")
        if self.constant is not None and (
            self.offset or self.at or self.missing != "fail" or self.fallback_basis
        ):
            raise ValueError("a constant takes no other option")
        if self.metric is not None and (
            self.missing != "fail"
            or self.fallback_basis
            or self.fallback
            or self.zero_is_placeholder_while_nonzero
        ):
            raise ValueError("missing, fallback and placeholder options apply to concepts only")
        return self


class MetricDef(_Model):
    """One metric as written in the registry."""

    version: str
    name: str
    description: str
    synonyms: list[str] = []
    inputs: dict[str, InputSpec]
    formula: str
    unit: Literal["RATIO", "INR", "DAYS", "TIMES", "INR_PER_SHARE"]
    display: Literal["percent", "percentage_points", "crore", "days", "times", "number"]
    period_types: list[PeriodType]
    sectors: list[SectorType] | None = None
    assumptions: list[str] = []
    limitations: list[str] = []
    helper: bool = False
    # the basis a reader should see first (M09 says which basis it used and offers the other);
    # both bases are always computed. Omitted = the company's default basis
    preferred_basis: Literal["consolidated", "standalone"] | None = None
    # inputs that are balances also look at the opening balance's validation results
    include_opening: bool = False

    @field_validator("period_types")
    @classmethod
    def _period_types(cls, value: list[PeriodType]) -> list[PeriodType]:
        if not value:
            raise ValueError("period_types must not be empty")
        if PeriodType.INSTANT in value and len(value) > 1:
            raise ValueError("INSTANT cannot be mixed with duration period types")
        return value


class RegistryFile(_Model):
    """Shape of the metrics registry."""

    version: int
    metrics: dict[str, MetricDef]


@dataclass(frozen=True)
class Metric:
    """A validated metric with its compiled formula."""

    id: str
    version: str
    name: str
    description: str
    synonyms: tuple[str, ...]
    inputs: dict[str, InputSpec]
    formula: Compiled
    unit: str
    display: str
    period_types: tuple[PeriodType, ...]
    sectors: tuple[SectorType, ...] | None
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]
    helper: bool
    include_opening: bool
    preferred_basis: str | None

    @property
    def is_instant(self) -> bool:
        """True for a balance-sheet metric (one value per date)."""
        return self.period_types == (PeriodType.INSTANT,)

    def applies_to(self, sector: str) -> bool:
        """True if the metric is defined for this sector type."""
        return self.sectors is None or sector in {s.value for s in self.sectors}


class MetricRegistry:
    """Validated metrics: lookup, dependency order, synonyms and descriptions."""

    def __init__(self, version: int, metrics: dict[str, Metric], order: list[str]) -> None:
        self.version = version
        self._metrics = metrics
        self._order = order

    def __contains__(self, metric_id: object) -> bool:
        return metric_id in self._metrics

    def get(self, metric_id: str) -> Metric:
        """Metric ID -> Metric (KeyError naming the ID if unknown)."""
        try:
            return self._metrics[metric_id]
        except KeyError:
            raise KeyError(f"unknown metric '{metric_id}'") from None

    def order(self) -> list[str]:
        """Metric IDs with every input metric before the metrics using it."""
        return list(self._order)

    def synonyms(self) -> dict[str, list[str]]:
        """Lower-case synonym or name -> metric IDs (metrics of different sectors may share one)."""
        found: dict[str, list[str]] = {}
        for metric in self._metrics.values():
            if metric.helper:
                continue
            for text in (metric.name, *metric.synonyms):
                found.setdefault(text.casefold(), [])
                if metric.id not in found[text.casefold()]:
                    found[text.casefold()].append(metric.id)
        return found

    def describe(self, metric_id: str) -> dict[str, Any]:
        """Everything a reader needs next to a number: formula, inputs, assumptions, limitations."""
        m = self.get(metric_id)
        return {
            "id": m.id,
            "name": m.name,
            "description": m.description,
            "version": m.version,
            "formula": m.formula.text,
            "unit": m.unit,
            "display": m.display,
            "period_types": [p.value for p in m.period_types],
            "sectors": [s.value for s in m.sectors] if m.sectors else None,
            "inputs": {
                alias: spec.model_dump(exclude_none=True, exclude_defaults=True)
                for alias, spec in m.inputs.items()
            },
            "assumptions": list(m.assumptions),
            "limitations": list(m.limitations),
            "helper": m.helper,
            "preferred_basis": m.preferred_basis,
        }


# --- validation ---------------------------------------------------------------------------------


def _kind(period_types: tuple[PeriodType, ...] | list[PeriodType]) -> str:
    """Period types -> 'instant' | 'duration'."""
    return "instant" if list(period_types) == [PeriodType.INSTANT] else "duration"


def _check_input(
    owner: str,
    alias: str,
    spec: InputSpec,
    definition: MetricDef,
    raw: RegistryFile,
    taxonomy: Taxonomy,
    problems: list[str],
) -> None:
    """Validate one input against the taxonomy and the other metrics."""
    where = f"metric '{owner}' input '{alias}'"
    kind = _kind(definition.period_types)
    if spec.concept is not None:
        concept = taxonomy.concepts.get(spec.concept)
        if concept is None:
            problems.append(f"{where}: unknown concept '{spec.concept}'")
            return
        source_kind = concept.period_kind
    elif spec.metric is not None:
        other = raw.metrics.get(spec.metric)
        if other is None:
            problems.append(f"{where}: unknown metric '{spec.metric}'")
            return
        source_kind = _kind(other.period_types)
        wanted = set(definition.period_types) if kind == "duration" else {PeriodType.INSTANT}
        if (
            source_kind == "duration"
            and kind == "duration"
            and not wanted <= set(other.period_types)
        ):
            problems.append(
                f"{where}: metric '{spec.metric}' is not defined for every period type of '{owner}'"
            )
    else:
        return
    if source_kind == "duration" and kind == "instant":
        problems.append(f"{where}: a duration input cannot feed an INSTANT metric")
    if source_kind == "instant" and kind == "duration" and spec.at is None:
        problems.append(f"{where}: an instant input of a duration metric needs `at`")
    if spec.at is not None and (source_kind == "duration" or kind == "instant"):
        problems.append(f"{where}: `at` applies to an instant input of a duration metric only")
    if spec.offset and kind == "instant" and spec.offset.periods:
        problems.append(f"{where}: an INSTANT metric can only be offset by years")
    if (
        spec.fallback_basis
        and spec.concept
        and not taxonomy.concepts[spec.concept].zero_is_placeholder
    ):
        problems.append(
            f"{where}: fallback_basis needs a zero_is_placeholder concept, "
            f"'{spec.concept}' is not one"
        )
    for name in (
        [spec.zero_is_placeholder_while_nonzero] if spec.zero_is_placeholder_while_nonzero else []
    ) + ([spec.fallback.concept, *spec.fallback.only_if_zero_or_absent] if spec.fallback else []):
        if name not in taxonomy.concepts:
            problems.append(f"{where}: unknown concept '{name}'")
    if spec.fallback and spec.fallback.concept in taxonomy.concepts:
        fb = taxonomy.concepts[spec.fallback.concept]
        if fb.period_kind != source_kind:
            problems.append(f"{where}: fallback concept '{spec.fallback.concept}' has another kind")
    for name in spec.fallback.only_if_zero_or_absent if spec.fallback else []:
        if name in taxonomy.concepts and taxonomy.concepts[name].period_kind != "instant":
            problems.append(f"{where}: only_if_zero_or_absent '{name}' must be an instant concept")


def _check_metric(
    metric_id: str,
    definition: MetricDef,
    raw: RegistryFile,
    taxonomy: Taxonomy,
    problems: list[str],
) -> Compiled | None:
    """Validate one metric; return its compiled formula (None if the formula is invalid)."""
    if not _ID.match(metric_id):
        problems.append(f"metric id '{metric_id}' must be lower-case words joined by underscores")
    for alias in definition.inputs:
        if not _ALIAS.match(alias) or alias in ("avg", "abs", "min", "max"):
            problems.append(f"metric '{metric_id}': '{alias}' is not a usable input alias")
    for alias, spec in definition.inputs.items():
        _check_input(metric_id, alias, spec, definition, raw, taxonomy, problems)
    try:
        compiled = compile_formula(definition.formula)
    except EvalError as exc:
        problems.append(f"metric '{metric_id}': {exc}")
        return None
    unknown = compiled.names - set(definition.inputs)
    if unknown:
        problems.append(f"metric '{metric_id}': formula uses undeclared {sorted(unknown)}")
    unused = set(definition.inputs) - compiled.names
    if unused:
        problems.append(
            f"metric '{metric_id}': inputs {sorted(unused)} are not used by the formula"
        )
    return compiled


def _order(raw: RegistryFile, problems: list[str]) -> list[str]:
    """Topological order of the metrics; a cycle is a problem naming its members."""
    deps = {
        mid: sorted({s.metric for s in m.inputs.values() if s.metric in raw.metrics})
        for mid, m in raw.metrics.items()
    }
    done: list[str] = []
    state: dict[str, int] = {}  # 1 = in progress, 2 = done

    def visit(node: str, trail: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            loop = [*trail[trail.index(node) :], node]
            problems.append("dependency cycle: " + " -> ".join(loop))
            return
        state[node] = 1
        for dep in deps[node]:
            visit(dep, [*trail, node])
        state[node] = 2
        done.append(node)

    for node in sorted(raw.metrics):
        visit(node, [])
    return done


def _check_synonyms(raw: RegistryFile, problems: list[str]) -> None:
    """Two metrics may share a synonym only if their sectors do not overlap."""
    owners: dict[str, list[str]] = {}
    for mid, m in raw.metrics.items():
        if m.helper:
            continue
        for text in {t.casefold() for t in (m.name, *m.synonyms)}:
            owners.setdefault(text, []).append(mid)
    for text, ids in owners.items():
        for i, first in enumerate(ids):
            for second in ids[i + 1 :]:
                a, b = raw.metrics[first].sectors, raw.metrics[second].sectors
                if a is None or b is None or set(a) & set(b):
                    problems.append(
                        f"synonym '{text}' is on both '{first}' and '{second}' for the same sector"
                    )


def parse_registry(data: dict[str, Any], taxonomy: Taxonomy | None = None) -> MetricRegistry:
    """Raw registry dict -> validated MetricRegistry (MetricsError listing every problem)."""
    try:
        raw = RegistryFile.model_validate(data)
    except ValidationError as exc:
        lines = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise MetricsError("invalid metric registry:\n  " + "\n  ".join(lines)) from exc
    taxonomy = taxonomy or load_taxonomy()
    problems: list[str] = []
    compiled: dict[str, Compiled] = {}
    for metric_id, definition in raw.metrics.items():
        formula = _check_metric(metric_id, definition, raw, taxonomy, problems)
        if formula is not None:
            compiled[metric_id] = formula
    order = _order(raw, problems)
    _check_synonyms(raw, problems)
    if problems:
        raise MetricsError("invalid metric registry:\n  " + "\n  ".join(problems))
    metrics = {
        mid: Metric(
            id=mid,
            version=d.version,
            name=d.name,
            description=d.description,
            synonyms=tuple(d.synonyms),
            inputs=d.inputs,
            formula=compiled[mid],
            unit=d.unit,
            display=d.display,
            period_types=tuple(d.period_types),
            sectors=tuple(d.sectors) if d.sectors else None,
            assumptions=tuple(d.assumptions),
            limitations=tuple(d.limitations),
            helper=d.helper,
            include_opening=d.include_opening,
            preferred_basis=d.preferred_basis,
        )
        for mid, d in raw.metrics.items()
    }
    return MetricRegistry(raw.version, metrics, order)


def load_metrics(taxonomy: Taxonomy | None = None) -> MetricRegistry:
    """Read the registry named `metrics` in settings -> validated MetricRegistry."""
    return parse_registry(load_registry("metrics"), taxonomy)
