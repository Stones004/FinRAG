"""Dense index per ISIN: normalised vectors in a numpy file, cosine search by dot product."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

VECTORS_FILE = "vectors.npy"
IDS_FILE = "ids.json"


@dataclass
class VectorIndex:
    """Vectors of the chunks of one ISIN; `fingerprints` tell which text each vector came from."""

    model: str
    ids: list[str]
    fingerprints: list[str]
    vectors: np.ndarray  # (n, dim) float32, rows of length 1

    def scores(self, query: np.ndarray) -> np.ndarray:
        """Query vector -> cosine similarity to every chunk."""
        if not len(self.ids):
            return np.zeros(0)
        return np.asarray(self.vectors @ query, dtype=float)


def top_ids(
    ids: Sequence[str], scores: np.ndarray, allowed: set[str], k: int, positive_only: bool = False
) -> list[tuple[str, float]]:
    """Best `k` (id, score) among the allowed ids, best first; ties keep index order."""
    order = np.argsort(-scores, kind="stable")
    out: list[tuple[str, float]] = []
    for position in order:
        if ids[position] not in allowed:
            continue
        if positive_only and scores[position] <= 0:
            break
        out.append((ids[position], float(scores[position])))
        if len(out) == k:
            break
    return out


def save_vectors(index: VectorIndex, folder: Path) -> None:
    """Write vectors.npy and ids.json (model name, ids, text fingerprints)."""
    folder.mkdir(parents=True, exist_ok=True)
    np.save(folder / VECTORS_FILE, index.vectors)
    (folder / IDS_FILE).write_text(
        json.dumps({"model": index.model, "ids": index.ids, "fingerprints": index.fingerprints}),
        encoding="utf-8",
    )


def load_vectors(folder: Path) -> VectorIndex | None:
    """<folder> -> index, or None when it has not been built."""
    if not (folder / VECTORS_FILE).exists() or not (folder / IDS_FILE).exists():
        return None
    meta = json.loads((folder / IDS_FILE).read_text(encoding="utf-8"))
    vectors = np.load(folder / VECTORS_FILE)
    return VectorIndex(meta["model"], meta["ids"], meta["fingerprints"], vectors)
