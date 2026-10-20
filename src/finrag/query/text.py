"""Question text handling: normalisation, a working copy that blanks understood spans, n-grams."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache

from finrag.core.config import get_settings
from finrag.query.intents import Intents

_TOKEN = re.compile(r"[a-z0-9&/%']+(?:\.[0-9]+)?")
_POSSESSIVE = re.compile(r"(?<=[a-z0-9])'s\b")
_STRIP = re.compile(r"[^a-z0-9&/%'.\s]")
_LONE_DOT = re.compile(r"(?<![0-9])\.|\.(?![0-9])")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Span:
    """A matched piece of the working text."""

    start: int
    end: int
    text: str


def normalize(question: str, intents: Intents | None = None) -> str:
    """Question -> lower case, contractions expanded, possessives and punctuation removed, padded.

    The result starts and ends with one space so that " vs " style keywords match at the edges.
    """
    text = question.lower().replace("’", "'").replace("‘", "'")
    if intents is not None:
        for short, long in intents.contractions.items():
            text = re.sub(rf"(?<![a-z0-9]){re.escape(short)}(?![a-z0-9])", long, text)
    text = _POSSESSIVE.sub("", text)
    text = text.replace("-", " ").replace("_", " ")
    text = _STRIP.sub(" ", text)
    text = _LONE_DOT.sub(" ", text)
    return f" {_SPACES.sub(' ', text).strip()} "


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Phrase -> regex that matches it as whole words (a phrase with edge spaces is literal)."""
    return _phrase_pattern(phrase)


@cache
def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    if phrase != phrase.strip():
        return re.compile(re.escape(phrase))
    return re.compile(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])")


class Working:
    """The normalised question with spans already understood replaced by spaces.

    Every resolver searches `work`, consumes what it understood and leaves the rest, so a later
    resolver cannot read the same words twice ("pat" inside "pat growth", "fy26" inside "q1 fy26").
    `text` keeps the unmasked normalised question for keyword checks.
    """

    def __init__(self, question: str, intents: Intents) -> None:
        self.original = question
        self.text = normalize(question, intents)
        self.work = self.text

    def find(self, phrase: str) -> list[Span]:
        """Phrase -> its whole-word matches in the unconsumed text."""
        return [
            Span(m.start(), m.end(), m.group()) for m in phrase_pattern(phrase).finditer(self.work)
        ]

    def search(self, pattern: re.Pattern[str]) -> list[re.Match[str]]:
        """Regex -> its matches in the unconsumed text."""
        return list(pattern.finditer(self.work))

    def consume(self, span: Span | re.Match[str]) -> None:
        """Blank a matched span (same length, so other positions stay valid)."""
        start, end = (span.start, span.end) if isinstance(span, Span) else span.span()
        self.work = self.work[:start] + " " * (end - start) + self.work[end:]

    def tokens(self) -> list[Span]:
        """The words still unconsumed, with their positions."""
        return [Span(m.start(), m.end(), m.group()) for m in _TOKEN.finditer(self.work)]

    def ngrams(self, min_chars: int, max_tokens: int) -> Iterator[Span]:
        """Runs of neighbouring unconsumed words (1..max_tokens) at least min_chars long."""
        tokens = self.tokens()
        for i in range(len(tokens)):
            for j in range(i, min(i + max_tokens, len(tokens))):
                if j > i and tokens[j].start - tokens[j - 1].end != 1:
                    break  # a consumed span lies between the words
                text = self.work[tokens[i].start : tokens[j].end]
                if len(text) >= min_chars:
                    yield Span(tokens[i].start, tokens[j].end, text)


@cache
def load_vocab() -> frozenset[str]:
    """The common-English word list (paths.vocab_common_en) as a set of lower-case words."""
    path = get_settings().paths.vocab_common_en
    return frozenset(
        w.strip().lower() for w in path.read_text(encoding="utf-8").splitlines() if w.strip()
    )


def reset_vocab_cache() -> None:
    """Drop the cached word list (tests point settings at other folders)."""
    load_vocab.cache_clear()
