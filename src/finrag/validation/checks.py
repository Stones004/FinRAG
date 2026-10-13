"""One pure check per rule type: (rule, facts at one period, tolerance) -> CheckResult."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from finrag.core.config import ToleranceSettings
from finrag.core.types import ValidationStatus
from finrag.facts.models import Fact
from finrag.validation.rules import (
    CrossPeriodRule,
    EqualityRule,
    RangeRule,
    SignRule,
    SumRule,
)

UNIT_INR = "INR"
Facts = Mapping[str, Fact]  # concept -> the current fact at the period being checked


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check: a status and the details stored with it."""

    status: ValidationStatus
    details: dict[str, Any] = field(default_factory=dict)


def gap(
    a: Decimal, b: Decimal, unit: str, tol: ToleranceSettings
) -> tuple[bool, Decimal, Decimal | None]:
    """Two values -> (exceeds tolerance, absolute difference, relative difference).

    Relative is against the larger magnitude. It fails only if BOTH tolerances are exceeded;
    the absolute (rupee) tolerance applies to INR values only.
    """
    diff = abs(a - b)
    larger = max(abs(a), abs(b))
    rel = None if larger == 0 else diff / larger
    rel_exceeded = rel is not None and rel > Decimal(str(tol.relative_tolerance))
    abs_exceeded = unit != UNIT_INR or diff > Decimal(tol.absolute_tolerance_inr)
    return bool(diff) and rel_exceeded and abs_exceeded, diff, rel


def _missing(names: list[str], facts: Facts) -> list[str]:
    """Names with no fact at this period."""
    return [n for n in names if n not in facts]


def _skip(missing: list[str]) -> CheckResult:
    """A check whose inputs are missing -> skip, naming them."""
    return CheckResult(ValidationStatus.SKIP, {"missing": missing})


def _text(value: Decimal | None) -> str | None:
    """Decimal -> plain text (money is never a float)."""
    return None if value is None else format(value, "f")


def _compare(
    rule: EqualityRule | CrossPeriodRule, left: Fact, right: Fact, tol: ToleranceSettings
) -> CheckResult:
    """Two facts that must be equal -> pass, or the rule's failure status with the gap."""
    failed, diff, rel = gap(left.value, right.value, left.unit, tol)
    details = {
        "left": _text(left.value),
        "right": _text(right.value),
        "abs_diff": _text(diff),
        "rel_diff": _text(rel),
        "sources": {"left": left.source_class.value, "right": right.source_class.value},
    }
    return CheckResult(rule.fail_status if failed else ValidationStatus.PASS, details)


def check_equality(rule: EqualityRule, facts: Facts, tol: ToleranceSettings) -> CheckResult:
    """equality / cross_statement: left = right at the same period."""
    missing = _missing([rule.left, rule.right], facts)
    if missing:
        return _skip(missing)
    result = _compare(rule, facts[rule.left], facts[rule.right], tol)
    result.details["concepts"] = [rule.left, rule.right]
    return result


def check_sum(rule: SumRule, facts: Facts, tol: ToleranceSettings) -> CheckResult:
    """sum: total = parts + optional parts (absent = 0) - minus."""
    missing = _missing([*rule.parts, *rule.minus, rule.total], facts)
    if missing:
        return _skip(missing)
    present = [p for p in rule.optional_parts if p in facts]
    total = facts[rule.total]
    computed = sum((facts[p].value for p in [*rule.parts, *present]), Decimal(0))
    computed -= sum((facts[m].value for m in rule.minus), Decimal(0))
    failed, diff, rel = gap(computed, total.value, total.unit, tol)
    used = [*rule.parts, *present, *rule.minus, rule.total]
    details = {
        "sum": _text(computed),
        "total": _text(total.value),
        "abs_diff": _text(diff),
        "rel_diff": _text(rel),
        "concepts": used,
        "sources": sorted({facts[c].source_class.value for c in used}),
    }
    return CheckResult(rule.fail_status if failed else ValidationStatus.PASS, details)


def check_sign(rule: SignRule, facts: Facts, tol: ToleranceSettings) -> CheckResult:
    """sign: the value has the expected sign (zero is fine)."""
    del tol
    if rule.concept not in facts:
        return _skip([rule.concept])
    value = facts[rule.concept].value
    bad = value < 0 if rule.expected == "non_negative" else value > 0
    details = {"value": _text(value), "expected": rule.expected, "concepts": [rule.concept]}
    return CheckResult(rule.fail_status if bad else ValidationStatus.PASS, details)


def check_range(rule: RangeRule, facts: Facts, tol: ToleranceSettings) -> CheckResult:
    """range: numerator / denominator lies within [min, max]."""
    del tol
    missing = _missing([rule.numerator, rule.denominator], facts)
    if missing:
        return _skip(missing)
    denominator = facts[rule.denominator].value
    if denominator == 0:
        return CheckResult(ValidationStatus.SKIP, {"reason": "denominator_zero"})
    ratio = facts[rule.numerator].value / denominator
    details = {
        "ratio": _text(ratio),
        "min": _text(rule.min),
        "max": _text(rule.max),
        "concepts": [rule.numerator, rule.denominator],
    }
    outside = not rule.min <= ratio <= rule.max
    return CheckResult(rule.fail_status if outside else ValidationStatus.PASS, details)


def check_cross_period(
    rule: CrossPeriodRule, opening: Fact | None, closing: Fact | None, tol: ToleranceSettings
) -> CheckResult:
    """cross_period: the opening balance at a date equals the closing balance at that date."""
    missing = [n for n, f in ((rule.opening, opening), (rule.closing, closing)) if f is None]
    if opening is None or closing is None:
        return _skip(missing)
    result = _compare(rule, opening, closing, tol)  # type: ignore[arg-type]
    result.details["concepts"] = [rule.opening, rule.closing]
    return result
