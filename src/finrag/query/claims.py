"""Evidence-bound claims: what the model returns, and the checks that decide which survive.

The model returns claims `{text, cites, quote}`. A claim is kept only if all of this holds:
  1. it cites at least one passage ([N..]) and only aliases that exist;
  2. its quote is a verbatim substring (whitespace and typographic punctuation folded) of a cited
     passage and long enough to prove something;
  3. every number in its text is in the quote or is the displayed value of a figure it cites;
  4. a causal word in its text is matched by a causal word in the quote;
  5. (period binding) the cited passages belong to the asked period, and the quote names no other
     period;
  6. (hedge) a quote that contains a hedge word ("around", "maybe", "expect" ...) is turned into a
     claim only if the claim keeps a hedge of its own;
  7. (speaker) "management" is said only of management's own turns and never of a figure; an
     analyst's turn is called an analyst's.
The first failed check is the reason a claim is dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from finrag.core.types import Period
from finrag.narrative.search import SearchHit
from finrag.query.formatter import numbers_in
from finrag.query.period_refs import period_refs

CLAIMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["claims"],
    "additionalProperties": False,
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "cites", "quote"],
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "cites": {"type": "array", "items": {"type": "string"}},
                    "quote": {"type": "string"},
                },
            },
        }
    },
}

DROP_NO_CITATION = "no_citation"
DROP_UNKNOWN_ALIAS = "unknown_alias"
DROP_NO_PASSAGE = "no_passage"
DROP_QUOTE_SHORT = "quote_too_short"
DROP_QUOTE_NOT_FOUND = "quote_not_in_passage"
DROP_NUMBER = "number_not_in_quote"
DROP_CAUSAL = "causal_word_not_in_quote"
DROP_HEDGE = "hedge_not_kept"
DROP_PASSAGE_PERIOD = "passage_other_period"
DROP_QUOTE_PERIOD = "quote_other_period"
DROP_FIGURE_TO_MANAGEMENT = "figure_attributed_to_management"
DROP_NOT_MANAGEMENT = "management_not_speaking"
DROP_ANALYST = "analyst_not_named"

_CITE_GROUP = re.compile(r"\[[^\]]*\]")
_ALIAS = re.compile(r"^[FN]\d+$")
_SPACES = re.compile(r"\s+")
_BEFORE_PUNCTUATION = re.compile(r"\s+([.!?,;])")
_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
_ANALYST_ROLE = "analyst"


@dataclass(frozen=True)
class Claim:
    """One statement of the explanation, the passages it rests on and the words it quotes."""

    text: str
    cites: tuple[str, ...]
    quote: str

    @property
    def bare_text(self) -> str:
        """The text without any [..] the model may have written into it."""
        spaced = _SPACES.sub(" ", _CITE_GROUP.sub("", self.text)).strip()
        return _BEFORE_PUNCTUATION.sub(lambda m: m.group(1), spaced)

    @property
    def sentence(self) -> str:
        """The text with its citations in square brackets before the full stop."""
        tags = "".join(f"[{a}]" for a in self.cites)
        body = self.bare_text
        stop = body[-1] if body and body[-1] in ".!?" else "."
        body = body[:-1] if body and body[-1] in ".!?" else body
        return f"{body} {tags}{stop}" if tags else f"{body}{stop}"

    def to_json(self) -> dict[str, Any]:
        """Claim -> plain dict for the query log."""
        return {"text": self.bare_text, "cites": list(self.cites), "quote": self.quote}


def parse_claims(data: Any) -> list[Claim]:
    """The schema-checked reply -> claims (cites de-duplicated, in the order given)."""
    return [
        Claim(
            str(item["text"]),
            tuple(dict.fromkeys(str(c).strip().strip("[]") for c in item["cites"])),
            str(item["quote"]),
        )
        for item in data["claims"]
    ]


@dataclass(frozen=True)
class ClaimRules:
    """The settings and registries a claim is checked against."""

    periods: tuple[Period, ...]  # the periods the figures are for
    bind_period: bool  # False for a forward-looking question: guidance names future periods
    causal_words: tuple[str, ...]
    management_terms: tuple[str, ...]
    analyst_terms: tuple[str, ...]
    management_roles: frozenset[str]
    min_quote_chars: int
    require_citation: bool = True
    check_numbers: bool = True
    hedge_words: tuple[str, ...] = ()


def fold(text: str) -> str:
    """Text -> whitespace collapsed and typographic quotes and dashes made plain."""
    return _SPACES.sub(" ", text.translate(_FOLD)).strip()


def _has_word(text: str, words: tuple[str, ...]) -> bool:
    """True if any of the words or phrases occurs in the text as whole words (case-insensitive)."""
    lowered = text.casefold()
    return any(re.search(rf"(?<![a-z]){re.escape(w.casefold())}(?![a-z])", lowered) for w in words)


_MAY_IN_A_DATE = re.compile(r"(?<![a-z])may\s+\d|\d\s+may(?![a-z])", re.IGNORECASE)


def _has_hedge(text: str, words: tuple[str, ...]) -> bool:
    """True if the text hedges (the month of May is not a hedge)."""
    return _has_word(_MAY_IN_A_DATE.sub(" ", text), words)


def _inside(start: date, end: date, periods: tuple[Period, ...]) -> bool:
    """True if [start, end] lies inside one of the periods."""
    return any(p.start <= start and end <= p.end for p in periods)


def check_claim(
    claim: Claim,
    passages: dict[str, SearchHit],
    figures: dict[str, set[Decimal]],
    rules: ClaimRules,
) -> str | None:
    """Claim -> None if it passes, else the reason it is dropped (first failed check)."""
    cites = claim.cites
    if not cites:
        return None if not rules.require_citation else DROP_NO_CITATION
    unknown = [a for a in cites if not _ALIAS.match(a) or (a not in passages and a not in figures)]
    if unknown:
        return f"{DROP_UNKNOWN_ALIAS}: {', '.join(unknown)}"
    cited = [passages[a] for a in cites if a in passages]
    if not cited:
        return DROP_NO_PASSAGE
    quote = fold(claim.quote)
    if len(quote) < rules.min_quote_chars:
        return DROP_QUOTE_SHORT
    if not any(quote in fold(hit.text) for hit in cited):
        return DROP_QUOTE_NOT_FOUND
    text = claim.bare_text
    if rules.check_numbers:
        allowed = set(numbers_in(quote))
        for alias in cites:
            allowed |= figures.get(alias, set())
        stray = [n for n in numbers_in(text) if n not in allowed]
        if stray:
            return f"{DROP_NUMBER}: {', '.join(format(n.normalize(), 'f') for n in stray)}"
    if _has_word(text, rules.causal_words) and not _has_word(quote, rules.causal_words):
        return DROP_CAUSAL
    if _has_hedge(quote, rules.hedge_words) and not _has_hedge(text, rules.hedge_words):
        return DROP_HEDGE
    if rules.bind_period and rules.periods:
        reason = _period_problem(cited, quote, rules.periods)
        if reason:
            return reason
    return _speaker_problem(claim, cited, rules)


def _period_problem(cited: list[SearchHit], quote: str, periods: tuple[Period, ...]) -> str | None:
    """Passages outside the asked periods, or a quote naming another period -> reason."""
    for hit in cited:
        if hit.period_start and hit.period_end:
            start, end = date.fromisoformat(hit.period_start), date.fromisoformat(hit.period_end)
            if not _inside(start, end, periods):
                return f"{DROP_PASSAGE_PERIOD}: {hit.period_label or hit.period_end}"
    for ref in period_refs(quote):
        if not _inside(ref.start, ref.end, periods):
            return f"{DROP_QUOTE_PERIOD}: {ref.text}"
    return None


def _speaker_problem(claim: Claim, cited: list[SearchHit], rules: ClaimRules) -> str | None:
    """'Management' on anything but management's turns, or an analyst's turn not called one."""
    text = claim.bare_text
    if _has_word(text, rules.management_terms):
        if any(not a.startswith("N") for a in claim.cites):
            return DROP_FIGURE_TO_MANAGEMENT
        if any((hit.speaker_role or "").casefold() not in rules.management_roles for hit in cited):
            return DROP_NOT_MANAGEMENT
    if any((hit.speaker_role or "").casefold() == _ANALYST_ROLE for hit in cited) and not _has_word(
        text, rules.analyst_terms
    ):
        return DROP_ANALYST
    return None
