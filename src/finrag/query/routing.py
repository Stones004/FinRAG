"""The routing test: run every case of the routing set through the intent parser and score it.

Cases marked `held_out` are never used to tune the registry, the resolvers or the prompts; their
accuracy is reported apart from the tuning cases. A *wrong-confident* case is a wrong intent that
the parser would have answered without asking (decision `proceed`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from finrag.core.config import REPO_ROOT, get_settings, read_yaml
from finrag.core.fiscal import period_to_label
from finrag.query.intent_parser import IntentParser
from finrag.query.models import PROCEED, Clarification, Intent

LATEST = "latest"


@dataclass
class CaseResult:
    """One case: did the parser read the question as expected?"""

    id: str
    question: str
    held_out: bool
    passed: bool
    wrong_confident: bool
    problems: list[str] = field(default_factory=list)
    got: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutingReport:
    """All case results with the numbers the acceptance criterion asks for."""

    results: list[CaseResult]

    def group(self, held_out: bool) -> list[CaseResult]:
        """The tuning cases (held_out False) or the held-out cases."""
        return [r for r in self.results if r.held_out is held_out]

    @staticmethod
    def rate(results: list[CaseResult]) -> float:
        """Share of cases that passed (1.0 for an empty group)."""
        return sum(r.passed for r in results) / len(results) if results else 1.0

    @property
    def wrong_confident(self) -> list[CaseResult]:
        """Cases read wrongly but above the auto-proceed threshold."""
        return [r for r in self.results if r.wrong_confident]

    def failures(self) -> list[CaseResult]:
        """Cases that did not pass."""
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        """Pass rates (tuning and held-out apart) and the wrong-confident count."""
        tuning, held = self.group(False), self.group(True)
        return (
            f"tuning cases:   {sum(r.passed for r in tuning)}/{len(tuning)} "
            f"({self.rate(tuning):.0%})\n"
            f"held-out cases: {sum(r.passed for r in held)}/{len(held)} ({self.rate(held):.0%})\n"
            f"wrong-confident: {len(self.wrong_confident)}"
        )


def load_cases(path: Path | None = None) -> dict[str, Any]:
    """Cases file -> its content (today's date, cases)."""
    return read_yaml(path or REPO_ROOT / get_settings().query.routing.cases)


def _labels(intent: Intent) -> list[str]:
    """The intent's periods as labels."""
    return [period_to_label(p) for p in intent.periods]


def _intent_problems(case: dict[str, Any], intent: Intent, isins: dict[str, str]) -> list[str]:
    """Differences between an expected intent and the parsed one."""
    expect = case["expect"]
    problems: list[str] = []
    if intent.entity_isin != isins[expect["isin"]]:
        problems.append(f"company {intent.entity_name}")
    if sorted(intent.metrics) != sorted(expect["metrics"]):
        problems.append(f"metrics {intent.metrics}")
    if expect["periods"] == [LATEST]:
        if "period" not in intent.defaults_applied:
            problems.append(f"periods {_labels(intent)} (expected the default)")
    elif _labels(intent) != expect["periods"]:
        problems.append(f"periods {_labels(intent)}")
    if intent.question_type != expect["question_type"]:
        problems.append(f"question_type {intent.question_type}")
    for key, got in (
        ("comparison", intent.comparison),
        ("basis", intent.basis),
    ):
        if key in expect and got != expect[key]:
            problems.append(f"{key} {got}")
    if "forward_looking" in expect and intent.forward_looking != expect["forward_looking"]:
        problems.append(f"forward_looking {intent.forward_looking}")
    if "exploratory" in expect and intent.exploratory != expect["exploratory"]:
        problems.append(f"exploratory {intent.exploratory}")
    if "management_only" in expect and intent.management_only != expect["management_only"]:
        problems.append(f"management_only {intent.management_only}")
    if "horizon" in expect and [period_to_label(p) for p in intent.horizon] != expect["horizon"]:
        problems.append(f"horizon {[period_to_label(p) for p in intent.horizon]}")
    if "sections" in expect and sorted(intent.sections) != sorted(expect["sections"]):
        problems.append(f"sections {intent.sections}")
    if "unavailable_sources" in expect and sorted(intent.unavailable_sources) != sorted(
        expect["unavailable_sources"]
    ):
        problems.append(f"unavailable_sources {intent.unavailable_sources}")
    return problems


def judge(case: dict[str, Any], got: Intent | Clarification, isins: dict[str, str]) -> CaseResult:
    """One case and the parser's result -> CaseResult."""
    held = bool(case.get("held_out", False))
    base = CaseResult(case["id"], case["q"], held, False, False)
    if isinstance(got, Clarification):
        base.got = {"clarify": got.reason}
        if "clarify" not in case:
            base.problems.append(f"clarified ({got.reason}) but an intent was expected")
        elif got.reason != case["clarify"]:
            base.problems.append(f"clarified {got.reason}, expected {case['clarify']}")
        base.passed = not base.problems
        return base
    base.got = {
        "isin": got.entity_isin,
        "metrics": got.metrics,
        "periods": _labels(got),
        "question_type": got.question_type,
        "comparison": got.comparison,
        "basis": got.basis,
        "decision": got.decision,
        "confidence": round(got.confidence, 2),
    }
    if "clarify" in case:
        base.problems.append(f"expected a clarification ({case['clarify']}), got an intent")
        base.wrong_confident = got.decision == PROCEED
        return base
    intent_problems = _intent_problems(case, got, isins)
    base.problems += intent_problems
    base.wrong_confident = bool(intent_problems) and got.decision == PROCEED
    wanted = case["expect"].get("decision")
    if wanted and got.decision != wanted:
        base.problems.append(f"decision {got.decision}, expected {wanted}")
    base.passed = not base.problems
    return base


def run_routing(
    parser: IntentParser, isins: dict[str, str], path: Path | None = None
) -> RoutingReport:
    """Every case of the routing set -> RoutingReport (`isins`: NSE symbol -> ISIN)."""
    data = load_cases(path)
    today = data["today"] if isinstance(data["today"], date) else date.fromisoformat(data["today"])
    return RoutingReport([judge(c, parser.parse(c["q"], today), isins) for c in data["cases"]])
