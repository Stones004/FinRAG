"""Passages -> numbered sentences, the unit the explain step chooses from.

The model is shown every retrieved passage split into sentences with ids ("N2.3" = the third
sentence of passage N2) and returns ids, never text. What is shown to the reader is the sentence
as stored, so a quote cannot be altered or invented. The split is done once, here, and used for
the prompt and for the check alike.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from finrag.narrative.search import SearchHit
from finrag.query.models import Evidence

# the speaker label every Q&A chunk starts with ("Answer (Name, Title) (continued): ")
_LABEL = re.compile(r"^(?:Question|Answer|Follow-up) \([^)]*\)(?: \(continued\))?:\s*")
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“‘(\[])")
_LIST_MARKER = re.compile(r"^\d{1,2}\.$")
_LEADING_MARKER = re.compile(r"^\d{1,2}\.\s+")
_LAST_WORD = re.compile(r"([A-Za-z.]+)\.$")


@dataclass(frozen=True)
class Sentence:
    """One numbered sentence of one retrieved passage."""

    id: str  # "N2.3"
    alias: str  # "N2"
    number: int  # 3
    text: str
    hit: SearchHit


def split_sentences(text: str, abbreviations: frozenset[str]) -> list[str]:
    """Passage text -> its sentences, in order, each exactly as written.

    The speaker label of a Q&A chunk and a list number ("4.") are not part of a sentence; no cut
    is made after an abbreviation or inside a number.
    """
    body = _LABEL.sub("", text, count=1)
    out: list[str] = []
    for piece in _BOUNDARY.split(body):
        piece = piece.strip()
        if not piece:
            continue
        if out and _glued_to_previous(out[-1], abbreviations):
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return [s for s in (_LEADING_MARKER.sub("", s, count=1) for s in out) if s]


def _glued_to_previous(previous: str, abbreviations: frozenset[str]) -> bool:
    """True if `previous` ends in a list number or an abbreviation (the next piece continues it)."""
    if _LIST_MARKER.match(previous):
        return True
    match = _LAST_WORD.search(previous)
    return bool(match) and match.group(1).casefold().rstrip(".") in abbreviations


def number_sentences(evidence: Evidence, abbreviations: frozenset[str]) -> dict[str, Sentence]:
    """Evidence -> every sentence of every retrieved passage, keyed by id, in passage order."""
    by_id = {h.chunk_id: h for h in evidence.chunks}
    out: dict[str, Sentence] = {}
    for alias, chunk_id in evidence.chunk_aliases.items():
        hit = by_id.get(chunk_id)
        if hit is None:
            continue
        for number, text in enumerate(split_sentences(hit.text, abbreviations), start=1):
            sentence_id = f"{alias}.{number}"
            out[sentence_id] = Sentence(sentence_id, alias, number, text, hit)
    return out
