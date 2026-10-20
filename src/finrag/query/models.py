"""Plain types of the query engine: intent, clarification, plan, evidence, answer."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from finrag.core.types import Period
from finrag.facts.models import Fact
from finrag.metrics.models import MetricValue
from finrag.narrative.search import SearchHit, SearchRequest

# Clarification reasons (closed list)
NO_ENTITY = "no_entity"
UNKNOWN_COMPANY = "unknown_company"
MULTI_ENTITY = "multi_entity"
MISSING_METRIC = "missing_metric"
UNKNOWN_METRIC = "unknown_metric"
AMBIGUOUS_METRIC = "ambiguous_metric"
LOW_CONFIDENCE = "low_confidence"
NO_PERIOD = "no_period"
PERIOD_NOT_AVAILABLE = "period_not_available"
NO_NARRATIVE_MATCH = "no_narrative_match"

# Decisions of the intent parser
PROCEED = "proceed"
CONFIRM = "confirm"


@dataclass(frozen=True)
class Candidate:
    """One possible reading offered to the user (or the LLM shortlist): an ID and its name."""

    id: str
    name: str
    score: float = 0.0
    description: str = ""


@dataclass(frozen=True)
class PeriodChoice:
    """A period the question asks for, as shown to the user."""

    period: Period
    label: str  # "Q1 FY27"
    description: str  # "Q1 FY27 (Apr-Jun 2026)"


@dataclass
class Intent:
    """What the question asks, with every field a closed ID (company, metric, period, basis)."""

    question: str
    entity_isin: str
    entity_name: str
    metrics: list[str]  # metric IDs and/or taxonomy concept IDs
    periods: list[Period]
    comparison: str | None  # yoy | sequential | periods | trend | basis
    question_type: str  # lookup | comparison | explanation | narrative
    basis: str | None  # the basis to show first (None for a basis comparison)
    sections: list[str]
    confidence: float
    decision: str  # proceed | confirm
    defaults_applied: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    unavailable_sources: list[str] = field(default_factory=list)
    field_confidence: dict[str, float] = field(default_factory=dict)
    interpretation: str = ""
    notes: list[str] = field(default_factory=list)  # e.g. a period that is not filed
    sector_type: str = "general"
    llm_used: bool = False
    forward_looking: bool = False  # guidance / outlook: `periods` are last actuals, not the horizon
    horizon: list[Period] = field(default_factory=list)  # the future periods the question names
    search_quarters: list[Period] = field(default_factory=list)  # calls searched when defaulted
    management_only: bool = False  # asks what management said: only management's turns are searched
    exploratory: bool = False  # no metric and no cue: the filed results text is searched as it is

    def to_json(self) -> dict[str, Any]:
        """Intent -> plain dict for the query log."""
        return {
            "question": self.question,
            "isin": self.entity_isin,
            "entity": self.entity_name,
            "metrics": self.metrics,
            "periods": [
                [p.start.isoformat(), p.end.isoformat(), p.type.value] for p in self.periods
            ],
            "comparison": self.comparison,
            "question_type": self.question_type,
            "basis": self.basis,
            "sections": self.sections,
            "confidence": round(self.confidence, 3),
            "decision": self.decision,
            "defaults_applied": self.defaults_applied,
            "unresolved": self.unresolved,
            "unavailable_sources": self.unavailable_sources,
            "field_confidence": self.field_confidence,
            "interpretation": self.interpretation,
            "notes": self.notes,
            "llm_used": self.llm_used,
            "forward_looking": self.forward_looking,
            "horizon": [
                [p.start.isoformat(), p.end.isoformat(), p.type.value] for p in self.horizon
            ],
            "management_only": self.management_only,
            "exploratory": self.exploratory,
            "search_quarters": [
                [p.start.isoformat(), p.end.isoformat(), p.type.value] for p in self.search_quarters
            ],
        }


BASIS_SOURCE_TEXT = {
    "named": "as asked",
    "metric_preferred": "preferred for this metric",
    "entity_default": "company default",
    "settings_default": "default",
}


def basis_phrase(intent: Intent) -> str:
    """Intent -> 'consolidated basis (company default)' / 'consolidated and standalone'."""
    if intent.comparison == "basis" or intent.basis is None:
        return "consolidated and standalone"
    source = next(
        (d.split(":", 1)[1] for d in intent.defaults_applied if d.startswith("basis:")), "named"
    )
    return f"{intent.basis} basis ({BASIS_SOURCE_TEXT.get(source, source)})"


@dataclass
class Clarification:
    """The question cannot be answered as asked: say why and offer candidates."""

    question: str
    reason: str
    message: str
    candidates: list[Candidate] = field(default_factory=list)
    confidence: float = 0.0
    partial: dict[str, Any] = field(default_factory=dict)
    query_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Clarification -> plain dict for the query log."""
        return {
            "question": self.question,
            "reason": self.reason,
            "message": self.message,
            "candidates": [c.id for c in self.candidates],
            "confidence": round(self.confidence, 3),
            "partial": self.partial,
        }


@dataclass
class MetricRequest:
    """One metric (or concept) at one period on one basis to fetch."""

    metric: str
    period: Period
    basis: str
    role: str = "main"  # main | prior_year | previous_period | other_basis


@dataclass
class Plan:
    """What to fetch, in order. Logged as JSON."""

    intent: Intent
    requests: list[MetricRequest] = field(default_factory=list)
    searches: list[SearchRequest] = field(default_factory=list)
    search_groups: list[dict[str, Any]] = field(
        default_factory=list
    )  # order + quarters, for the log
    unavailable_sources: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Plan -> plain dict (the source order and the quarters searched are in `searches`)."""
        return {
            "requests": [
                {
                    "metric": r.metric,
                    "period": [
                        r.period.start.isoformat(),
                        r.period.end.isoformat(),
                        r.period.type.value,
                    ],
                    "basis": r.basis,
                    "role": r.role,
                }
                for r in self.requests
            ],
            "searches": self.search_groups,
            "unavailable_sources": self.unavailable_sources,
        }


@dataclass
class Entry:
    """One requested figure and what was found for it (a metric value, a fact, or nothing)."""

    request: MetricRequest
    alias: str | None = None  # F1.. when a value exists
    metric_value: MetricValue | None = None
    fact: Fact | None = None
    restatements: list[dict[str, Any]] = field(default_factory=list)
    gap: str | None = None  # why there is nothing: "never computed", "no balance sheet at ..."


@dataclass
class Evidence:
    """What was retrieved, with local aliases F1..Fn (facts, metric values) and N1..Nn (chunks)."""

    entries: list[Entry] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    metric_values: list[MetricValue] = field(default_factory=list)
    chunks: list[SearchHit] = field(default_factory=list)
    fact_aliases: dict[str, str] = field(default_factory=dict)  # alias -> fact_id
    metric_aliases: dict[str, str] = field(default_factory=dict)  # alias -> metric_value_id
    chunk_aliases: dict[str, str] = field(default_factory=dict)  # alias -> chunk_id
    restatements: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # fact_id -> rows
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    transcripts_missing: bool = False
    top_scores: dict[str, float] = field(
        default_factory=dict
    )  # scores of the best hit of the first search


@dataclass(frozen=True)
class Citation:
    """An alias mapped back to a real ID and what a reader needs to find it."""

    alias: str
    kind: str  # fact | metric | chunk
    id: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Answer:
    """The final answer: numbers from the template, explanation from the checked LLM text."""

    text: str
    numbers_block: str
    explanation: str
    citations: list[Citation]
    interpretation: str
    warnings: list[str] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)  # kept claims: text, cites, quote
    query_id: str | None = None
    values: dict[str, Decimal] = field(default_factory=dict)
