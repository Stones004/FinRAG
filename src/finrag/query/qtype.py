"""Question type and section hints from the keywords of the intents registry."""

from __future__ import annotations

from dataclasses import dataclass, field

from finrag.query.intents import Intents
from finrag.query.text import Working, phrase_pattern


@dataclass
class QuestionKind:
    """Type (None when the question gives no sign), section hints, sources not searched in v1."""

    question_type: str | None
    sections: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)  # types with a keyword hit, for the log
    forward: bool = False  # a forward-looking word: guidance, outlook, plan ...
    management: bool = False  # asks what management said


def _has(text: str, phrase: str) -> bool:
    """Keyword in the normalised text (a keyword with edge spaces is matched literally)."""
    return phrase_pattern(phrase).search(text) is not None


def classify_question(
    work: Working,
    intents: Intents,
    has_metric: bool,
    comparison_signal: bool,
) -> QuestionKind:
    """Question -> type by `question_type_order` (first type with a match wins).

    A comparison word, two named periods or a multi-year period (`comparison_signal`) count as a
    comparison match. A lookup that names no metric but a section is a narrative question. No
    match: a metric gives `default_question_type_with_metric`; a section hint
    or unavailable source gives `default_question_type_with_section_hint`; otherwise None.
    """
    text = work.text
    matched = [
        qtype
        for qtype in intents.question_type_order
        if any(_has(text, kw) for kw in intents.question_types.get(qtype, []))
    ]
    if comparison_signal and "comparison" not in matched:
        matched.append("comparison")
    sections: list[str] = []
    unavailable: list[str] = []
    for section, words in intents.section_hints.items():
        if any(_has(text, w) for w in words):
            (unavailable if section in intents.unavailable_sections else sections).append(section)
    kind = QuestionKind(None, sections, unavailable, matched)
    kind.management = any(_has(text, w) for w in intents.management_words)
    if any(_has(text, w) for w in intents.forward_words):
        kind.forward = True
        kind.management = True  # guidance is what management said, not what analysts asked
        kind.question_type = "narrative"
        return kind
    for qtype in intents.question_type_order:
        if qtype in matched:
            kind.question_type = qtype
            if qtype == "lookup" and not has_metric and (sections or unavailable):
                kind.question_type = intents.default_question_type_with_section_hint
            return kind
    if has_metric:
        kind.question_type = intents.default_question_type_with_metric
    elif sections or unavailable:
        kind.question_type = intents.default_question_type_with_section_hint
    return kind
