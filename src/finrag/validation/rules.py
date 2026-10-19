"""Load and validate the validation-rule registry (one pydantic model per rule type)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from finrag.core.config import load_registry
from finrag.core.types import SectorType, ValidationStatus
from finrag.normalize.taxonomy import Taxonomy, load_taxonomy

KIND_INSTANT = "instant"
KIND_DURATION = "duration"
INSTANT_KINDS = ("instant", "instant_at_period_end")


class RulesError(Exception):
    """Raised when the rule registry is invalid; the message names every problem found."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(_Model):
    """total = sum(parts) + sum(optional_parts, missing = 0) - sum(minus)."""

    id: str
    parts: list[str]
    optional_parts: list[str] = []
    minus: list[str] = []
    total: str

    def concepts(self) -> list[str]:
        """Every concept the identity uses."""
        return [*self.parts, *self.optional_parts, *self.minus, self.total]


class _Base(_Model):
    id: str
    description: str = ""
    severity_on_fail: Literal["warn", "block"] = "warn"
    sectors: list[SectorType] | None = None  # None = every sector
    blocks_concepts: list[str] = []

    @property
    def fail_status(self) -> ValidationStatus:
        """Status a failed check gets."""
        return ValidationStatus(self.severity_on_fail)

    def concepts(self) -> list[str]:
        """Every concept the rule reads."""
        raise NotImplementedError


class EqualityRule(_Base):
    type: Literal["equality"]
    left: str
    right: str
    period_kind: Literal["instant", "duration", "instant_at_period_end"] | None = None

    def concepts(self) -> list[str]:
        return [self.left, self.right]


class CrossStatementRule(EqualityRule):
    type: Literal["cross_statement"]  # type: ignore[assignment]


class SumRule(_Base):
    type: Literal["sum"]
    parts: list[str]
    optional_parts: list[str] = []
    minus: list[str] = []
    total: str
    period_kind: Literal["instant", "duration"] | None = None

    def concepts(self) -> list[str]:
        return [*self.parts, *self.optional_parts, *self.minus, self.total]


class SignRule(_Base):
    type: Literal["sign"]
    concept: str
    expected: Literal["non_negative", "non_positive"]

    def concepts(self) -> list[str]:
        return [self.concept]


class RangeRule(_Base):
    type: Literal["range"]
    numerator: str
    denominator: str
    min: Decimal
    max: Decimal

    def concepts(self) -> list[str]:
        return [self.numerator, self.denominator]


class CrossPeriodRule(_Base):
    type: Literal["cross_period"]
    opening: str
    closing: str

    def concepts(self) -> list[str]:
        return [self.opening, self.closing]


class CrossSourceRule(_Base):
    type: Literal["cross_source"]
    identities: list[Identity] = []

    def concepts(self) -> list[str]:
        """Concepts named by the identities (the comparison itself covers every stored key)."""
        return [c for identity in self.identities for c in identity.concepts()]


Rule = Annotated[
    EqualityRule
    | CrossStatementRule
    | SumRule
    | SignRule
    | RangeRule
    | CrossPeriodRule
    | CrossSourceRule,
    Field(discriminator="type"),
]


class GapFillGate(_Model):
    """The M05 gap-fill gate: ids of the cross_source identities a PDF/OCR column must satisfy."""

    identities: list[str]


class RulesFile(_Model):
    """Shape of the rule registry."""

    version: int
    rules: list[Rule]
    gap_fill_gate: GapFillGate | None = None


@dataclass(frozen=True)
class Rules:
    """Validated rules, in file order, with the period kind each one works on."""

    version: int
    rules: tuple[Rule, ...]
    kinds: dict[str, str]  # rule id -> instant | duration (cross_source: duration, unused)

    def get(self, rule_id: str) -> Rule | None:
        """Rule id -> rule, or None."""
        return next((r for r in self.rules if r.id == rule_id), None)

    def applies_to(self, rule: Rule, sector: str) -> bool:
        """True if the rule is not limited to other sector types."""
        return rule.sectors is None or sector in {s.value for s in rule.sectors}


def _kind_of(concepts: list[str], taxonomy: Taxonomy) -> str | None:
    """Concepts -> their common period kind, or None when they differ."""
    kinds = {taxonomy.concepts[c].period_kind for c in concepts}
    return kinds.pop() if len(kinds) == 1 else None


def _check_rule(rule: Rule, taxonomy: Taxonomy, problems: list[str]) -> str:
    """Check one rule against the taxonomy; append problems, return its period kind."""
    here = f"rule '{rule.id}'"
    names = [*rule.concepts(), *rule.blocks_concepts]
    unknown = sorted({n for n in names if n not in taxonomy.concepts})
    for name in unknown:
        problems.append(f"{here}: unknown concept '{name}'")
    if unknown:
        return KIND_DURATION
    if rule.severity_on_fail == "block" and not rule.blocks_concepts:
        problems.append(f"{here}: severity block needs blocks_concepts")
    if rule.severity_on_fail == "warn" and rule.blocks_concepts:
        problems.append(f"{here}: blocks_concepts is only for severity block")
    if isinstance(rule, CrossSourceRule):
        return KIND_DURATION
    kind = _kind_of(rule.concepts(), taxonomy)
    if isinstance(rule, CrossStatementRule | CrossPeriodRule):
        kind = KIND_INSTANT if kind == KIND_INSTANT else None
    if kind is None:
        problems.append(f"{here}: its concepts do not share one period kind")
        return KIND_DURATION
    declared = getattr(rule, "period_kind", None)
    if declared and (declared in INSTANT_KINDS) != (kind == KIND_INSTANT):
        problems.append(f"{here}: period_kind '{declared}' does not match its concepts ({kind})")
    return kind


def parse_rules(data: dict[str, Any], taxonomy: Taxonomy) -> Rules:
    """Raw registry dict -> validated Rules (RulesError listing every problem)."""
    try:
        raw = RulesFile.model_validate(data)
    except ValidationError as exc:
        lines = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise RulesError("invalid validation rules:\n  " + "\n  ".join(lines)) from exc
    problems: list[str] = []
    kinds: dict[str, str] = {}
    for rule in raw.rules:
        if rule.id in kinds:
            problems.append(f"duplicate rule id '{rule.id}'")
        kinds[rule.id] = _check_rule(rule, taxonomy, problems)
    if problems:
        raise RulesError("invalid validation rules:\n  " + "\n  ".join(problems))
    return Rules(raw.version, tuple(raw.rules), kinds)


def load_rules(taxonomy: Taxonomy | None = None) -> Rules:
    """Read the registry named `validation_rules` in settings -> validated Rules."""
    return parse_rules(load_registry("validation_rules"), taxonomy or load_taxonomy())
