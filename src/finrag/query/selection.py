"""The explain step as a selection: the model picks numbered sentences, the program checks them.

What the reader sees is the stored sentence, so there is no quote to forge. Two kinds of check:
  * on the sentence (the pick is dropped): the id exists and is picked once, the sentence is long
    enough to say something, and it is about the period asked (the passage and the periods the
    sentence itself names);
  * on the lead-in, the few words the model may put before the sentence (the lead-in is dropped
    and the bare sentence is shown): a number, a period label or an entity in it must be in the
    sentence or in a figure shown; a cause word needs a cause word in the sentence; a hedge in the
    sentence needs a hedge in the lead-in; "management" and "analyst" only for the right speaker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from finrag.query.claims import (
    ClaimRules,
    _has_hedge,
    _has_word,
    _inside,
    _period_problem,
    fold,
)
from finrag.query.formatter import numbers_in
from finrag.query.period_refs import period_refs
from finrag.query.sentences import Sentence

SELECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["selections"],
    "additionalProperties": False,
    "properties": {
        "selections": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "lead_in": {"type": "string"}},
            },
        }
    },
}

DROP_UNKNOWN_SENTENCE = "unknown_sentence"
DROP_DUPLICATE = "duplicate_sentence"
DROP_SENTENCE_SHORT = "sentence_too_short"

LEAD_NUMBER = "number_not_in_sentence"
LEAD_PERIOD = "period_not_in_sentence"
LEAD_ENTITY = "entity_not_in_sentence"
LEAD_CAUSAL = "causal_word_not_in_sentence"
LEAD_HEDGE = "hedge_not_kept"
LEAD_NOT_MANAGEMENT = "management_not_speaking"

_ANALYST_ROLE = "analyst"
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&'\-]*")
_PERIOD_WORD = re.compile(r"^(?:[QH][1-4]|9M|FY\d{2,4})$", re.I)
_POSSESSIVE = re.compile(r"'s$", re.I)
_MONTHS = frozenset(
    "january february march april may june july august september october november december".split()
)


@dataclass(frozen=True)
class Selection:
    """One pick of the model: a sentence id and an optional lead-in."""

    id: str
    lead_in: str = ""


def parse_selections(data: Any) -> list[Selection]:
    """The schema-checked reply -> selections in the order given."""
    return [
        Selection(str(item["id"]).strip().strip("[]"), str(item.get("lead_in") or "").strip())
        for item in data["selections"]
    ]


@dataclass(frozen=True)
class Picked:
    """A sentence that passed, with the lead-in that survived (empty if none) and who said it."""

    sentence: Sentence
    lead_in: str = ""

    def to_json(self) -> dict[str, Any]:
        """Picked -> plain dict for the query log."""
        return {"id": self.sentence.id, "lead_in": self.lead_in, "sentence": self.sentence.text}


@dataclass(frozen=True)
class LeadInContext:
    """What a lead-in may draw on besides its sentence: the figures shown and the question."""

    figure_numbers: frozenset[Decimal] = frozenset()
    figure_words: frozenset[str] = frozenset()  # words of the figure lines (metric names, ...)
    known_names: frozenset[str] = frozenset()  # the company asked about
    vocab: frozenset[str] = (
        frozenset()
    )  # common English words (a capital letter alone is no entity)


def _tokens(text: str) -> set[str]:
    return {_POSSESSIVE.sub("", w).casefold() for w in _WORD.findall(text)}


def _entities(lead_in: str, vocab: frozenset[str]) -> list[str]:
    """Words of a lead-in that name something: a capital inside the lead-in, an acronym, or a
    capitalised first word that is not an ordinary English word."""
    out = []
    for i, raw in enumerate(_WORD.findall(lead_in)):
        word = _POSSESSIVE.sub("", raw)
        if _PERIOD_WORD.match(word) or word.casefold() in _MONTHS:
            continue
        acronym = word.isupper() and len(word) >= 2
        capital = word[0].isupper() and (i > 0 or word.casefold() not in vocab)
        if acronym or capital:
            out.append(word)
    return out


def sentence_problem(
    pick: Selection,
    sentences: dict[str, Sentence],
    seen: set[str],
    rules: ClaimRules,
) -> str | None:
    """Pick -> the reason it is dropped, or None."""
    sentence = sentences.get(pick.id)
    if sentence is None:
        return f"{DROP_UNKNOWN_SENTENCE}: {pick.id}"
    if pick.id in seen:
        return DROP_DUPLICATE
    text = fold(sentence.text)
    if len(text) < rules.min_quote_chars:
        return DROP_SENTENCE_SHORT
    if rules.bind_period and rules.periods:
        return _period_problem([sentence.hit], text, rules.periods)
    return None


def lead_in_problem(
    lead_in: str, sentence: Sentence, rules: ClaimRules, context: LeadInContext
) -> str | None:
    """Lead-in -> the reason it is dropped (the bare sentence is shown), or None."""
    text = fold(sentence.text)
    allowed = set(numbers_in(text)) | set(context.figure_numbers)
    stray = [n for n in numbers_in(lead_in) if n not in allowed]
    if stray:
        return f"{LEAD_NUMBER}: {', '.join(format(n.normalize(), 'f') for n in stray)}"
    in_sentence = {(r.start, r.end) for r in period_refs(text)}
    for ref in period_refs(lead_in):
        if (ref.start, ref.end) not in in_sentence and not _inside(
            ref.start, ref.end, rules.periods
        ):
            return f"{LEAD_PERIOD}: {ref.text}"
    known = _tokens(text) | context.figure_words | context.known_names
    known |= {t for w in rules.management_terms + rules.analyst_terms for t in _tokens(w)}
    missing = [
        w
        for w in _entities(lead_in, context.vocab)
        if _POSSESSIVE.sub("", w).casefold() not in known
    ]
    if missing:
        return f"{LEAD_ENTITY}: {', '.join(missing)}"
    if _has_word(lead_in, rules.causal_words) and not _has_word(text, rules.causal_words):
        return LEAD_CAUSAL
    if _has_hedge(text, rules.hedge_words) and not _has_hedge(lead_in, rules.hedge_words):
        return LEAD_HEDGE
    if _has_word(lead_in, rules.management_terms):
        role = (sentence.hit.speaker_role or "").casefold()
        if role not in rules.management_roles:
            return LEAD_NOT_MANAGEMENT
    return None


def attribution(sentence: Sentence, management_roles: frozenset[str]) -> str:
    """Who said a sentence, from the stored speaker (never from the model)."""
    hit = sentence.hit
    if hit.source_type != "transcript":
        return "Results filing, " + (hit.section or "text").replace("_", " ")
    role = (hit.speaker_role or "").casefold()
    who = hit.speaker or "speaker not identified"
    if role in management_roles:
        return f"Management ({who})"
    if role == _ANALYST_ROLE:
        firm = f", {hit.firm}" if hit.firm else ""
        return f"An analyst ({who}{firm})"
    return f"Earnings call ({who})"
