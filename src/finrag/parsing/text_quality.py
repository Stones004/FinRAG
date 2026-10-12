"""Score how much of a page's text is real words (separates good text layers from bad ones)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from finrag.core.config import ConfigError, Settings, get_settings
from finrag.parsing.sections import load_section_patterns

_TOKEN = re.compile(r"[A-Za-z]+")
# Regular endings tried when a token is not listed as is: the list holds base words only.
_SUFFIXES = ("s", "es", "d", "ed", "ing", "ly")


@dataclass(frozen=True)
class Vocabulary:
    """Known words: the common-English list plus the financial terms from the sections registry."""

    words: frozenset[str]
    min_token_len: int

    def knows(self, token: str) -> bool:
        """Lowercase token -> True if it, or it minus a regular ending, is a known word."""
        if token in self.words:
            return True
        for suffix in _SUFFIXES:
            stem = token.removesuffix(suffix) if token.endswith(suffix) else None
            if (
                stem
                and len(stem) >= self.min_token_len
                and (stem in self.words or f"{stem}e" in self.words)
            ):
                return True
        return False


def load_vocabulary(settings: Settings | None = None) -> Vocabulary:
    """Read the word list file and the financial terms -> Vocabulary."""
    settings = settings or get_settings()
    path = settings.paths.vocab_common_en
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"vocabulary file not found: {path}") from exc
    common = {line.strip().lower() for line in text.splitlines() if line.strip()}
    terms = load_section_patterns(settings).financial_terms
    return Vocabulary(frozenset(common | terms), settings.parsing.pdf.quality_min_token_len)


def text_quality(text: str, vocabulary: Vocabulary) -> float:
    """Page text -> share (0..1) of alphabetic tokens (length >= min) found in the vocabulary.

    A page with no such tokens scores 0.0: its quality cannot be confirmed.
    """
    tokens = [t.lower() for t in _TOKEN.findall(text) if len(t) >= vocabulary.min_token_len]
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if vocabulary.knows(t)) / len(tokens)
