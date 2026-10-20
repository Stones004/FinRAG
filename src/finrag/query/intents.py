"""The intents registry (registry key `intents`): schema, validation and loading."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finrag.core.config import ConfigError, load_registry
from finrag.normalize.taxonomy import Taxonomy, load_taxonomy

QUESTION_TYPES = ("lookup", "comparison", "explanation", "narrative")
PERIOD_KINDS = (
    "quarter",
    "half",
    "nine_months",
    "fiscal_year",
    "last_n_years",
    "last_quarter",
    "last_fiscal_year",
)
COMPARISONS = ("yoy", "sequential", "basis")
BASES = ("consolidated", "standalone")
CONFIDENCE_LEVELS = ("high", "medium", "low")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PeriodPattern(_Model):
    regex: str
    kind: str


class Intents(_Model):
    """The validated intents registry."""

    version: int
    question_type_order: list[str]
    question_types: dict[str, list[str]]
    default_question_type_with_metric: str
    default_question_type_with_section_hint: str
    llm_confidence_map: dict[str, float]
    forward_words: list[str]
    management_words: list[str]
    management_speaker_roles: list[str]
    causal_words: list[str]
    hedge_words: list[str]
    explore_stopwords: list[str]
    speaker_terms: dict[str, list[str]]
    growth_words: list[str]
    growth_counterparts: dict[str, str]
    default_results_sections: list[str]
    audit_sections: list[str]
    question_starters: list[str]
    contractions: dict[str, str]
    latest_words: list[str]
    basis_words: dict[str, list[str]]
    period_patterns: list[PeriodPattern]
    comparison_words: dict[str, list[str]]
    trend_words: list[str]
    concept_synonyms: dict[str, list[str]]
    section_hints: dict[str, list[str]]
    unavailable_sections: list[str] = Field(default_factory=list)
    topical_section_hints: list[str] = Field(default_factory=list)
    companions: dict[str, list[str]] = Field(default_factory=dict)
    unavailable_names: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> Intents:
        """Closed vocabularies, compilable regexes, no phrase owned by two concepts."""
        problems: list[str] = []
        order = self.question_type_order
        if set(order) != set(QUESTION_TYPES) or len(order) != len(QUESTION_TYPES):
            problems.append(f"question_type_order must list each of {QUESTION_TYPES} once")
        extra = set(self.question_types) - set(QUESTION_TYPES)
        if extra:
            problems.append(f"unknown question types: {sorted(extra)}")
        for key in (
            self.default_question_type_with_metric,
            self.default_question_type_with_section_hint,
        ):
            if key not in QUESTION_TYPES:
                problems.append(f"unknown default question type '{key}'")
        if set(self.llm_confidence_map) != set(CONFIDENCE_LEVELS):
            problems.append(f"llm_confidence_map must have keys {CONFIDENCE_LEVELS}")
        if set(self.basis_words) != set(BASES):
            problems.append(f"basis_words must have keys {BASES}")
        extra = set(self.comparison_words) - set(COMPARISONS)
        if extra:
            problems.append(f"unknown comparison kinds: {sorted(extra)}")
        for pattern in self.period_patterns:
            if pattern.kind not in PERIOD_KINDS:
                problems.append(f"unknown period kind '{pattern.kind}'")
            try:
                re.compile(pattern.regex)
            except re.error as exc:
                problems.append(f"bad period regex {pattern.regex!r}: {exc}")
        for name in ("default_results_sections", "audit_sections"):
            extra = set(getattr(self, name)) - set(self.section_hints)
            if extra:
                problems.append(f"{name} names unknown sections: {sorted(extra)}")
        if set(self.speaker_terms) != {"management", "analyst"}:
            problems.append("speaker_terms must have the keys management and analyst")
        if set(self.default_results_sections) & set(self.audit_sections):
            problems.append("default_results_sections must not include the audit sections")
        for name in ("unavailable_sections", "topical_section_hints"):
            extra = set(getattr(self, name)) - set(self.section_hints)
            if extra:
                problems.append(f"{name} without section_hints: {sorted(extra)}")
        owner: dict[str, str] = {}
        for concept, words in self.concept_synonyms.items():
            for word in words:
                key = word.casefold()
                if owner.setdefault(key, concept) != concept:
                    problems.append(f"'{word}' is a synonym of both {owner[key]} and {concept}")
        if problems:
            raise ValueError("; ".join(problems))
        return self


def parse_intents(data: dict[str, Any], taxonomy: Taxonomy | None = None) -> Intents:
    """Raw dict -> Intents; concept_synonyms must name taxonomy concepts."""
    try:
        intents = Intents.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"invalid intents registry: {exc}") from exc
    taxonomy = taxonomy or load_taxonomy()
    unknown = sorted(set(intents.concept_synonyms) - set(taxonomy.concepts))
    if unknown:
        raise ConfigError(f"intents.concept_synonyms names concepts not in the taxonomy: {unknown}")
    return intents


def load_intents(taxonomy: Taxonomy | None = None) -> Intents:
    """The registry named `intents` in settings -> validated Intents."""
    return parse_intents(load_registry("intents"), taxonomy)
