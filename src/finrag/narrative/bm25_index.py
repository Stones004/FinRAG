"""Keyword index (BM25) per ISIN: tokenised corpus on disk, scores for a query."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

BM25_FILE = "bm25.json"
_TOKEN = re.compile(r"[a-z0-9]+(?:[-.,/][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    """Lower-case terms; numbers (7,737.02) and hyphenated terms stay whole, and a hyphenated
    word also adds its parts, so 'ind-as' is found by 'ind as' and by 'ind-as'."""
    tokens: list[str] = []
    for match in _TOKEN.findall(text.lower()):
        tokens.append(match)
        if "-" in match and not match.replace("-", "").replace(".", "").replace(",", "").isdigit():
            tokens.extend(part for part in match.split("-") if part)
    return tokens


@dataclass
class Bm25Index:
    """BM25 over the chunks of one ISIN (ids and their tokens)."""

    ids: list[str]
    corpus: list[list[str]]

    def __post_init__(self) -> None:
        from rank_bm25 import BM25Okapi

        self._bm25 = BM25Okapi(self.corpus) if self.corpus else None

    def scores(self, query: str) -> np.ndarray:
        """Query -> one BM25 score per indexed chunk (zeros when nothing matches)."""
        if self._bm25 is None:
            return np.zeros(0)
        return np.asarray(self._bm25.get_scores(tokenize(query)), dtype=float)


def build_bm25(ids: Sequence[str], texts: Sequence[str]) -> Bm25Index:
    """Chunk ids and texts -> an index."""
    return Bm25Index(list(ids), [tokenize(t) for t in texts])


def save_bm25(index: Bm25Index, folder: Path) -> None:
    """Write the tokenised corpus to <folder>/bm25.json."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / BM25_FILE).write_text(
        json.dumps({"ids": index.ids, "corpus": index.corpus}), encoding="utf-8"
    )


def load_bm25(folder: Path) -> Bm25Index | None:
    """<folder>/bm25.json -> index, or None when it has not been built."""
    path = folder / BM25_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Bm25Index(data["ids"], data["corpus"])
