"""Build the BM25 and vector indexes of one ISIN from its stored chunks.

The indexes are rebuilt from the `chunks` table (small at watch-list scale); vectors whose text
and model are unchanged are reused, so only new or changed chunks are embedded again.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy.engine import Engine

from finrag.core.config import Settings
from finrag.core.db import transaction
from finrag.narrative import store
from finrag.narrative.bm25_index import BM25_FILE, build_bm25, save_bm25
from finrag.narrative.embedder import Embedder
from finrag.narrative.vector_index import (
    IDS_FILE,
    VECTORS_FILE,
    VectorIndex,
    load_vectors,
    save_vectors,
)


def index_folder(settings: Settings, isin: str) -> Path:
    """Folder holding the indexes of one ISIN."""
    return settings.paths.indexes / isin


def fingerprint(text: str) -> str:
    """Text -> short hash that says whether a vector is still valid."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def rebuild_isin(
    isin: str,
    embedder: Embedder,
    settings: Settings,
    engine: Engine | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Rebuild the indexes of one ISIN -> counts (chunks, embedded, reused)."""
    with transaction(engine) as conn:
        items = store.texts_for_isin(conn, isin)
    folder = index_folder(settings, isin)
    if not items:  # no chunks left: no indexes
        if folder.exists():
            for name in (BM25_FILE, VECTORS_FILE, IDS_FILE):
                (folder / name).unlink(missing_ok=True)
            if not any(folder.iterdir()):
                shutil.rmtree(folder)
        return {"isin": isin, "chunks": 0, "embedded": 0, "reused": 0}
    ids = [cid for cid, _ in items]
    texts = [text for _, text in items]
    prints = [fingerprint(t) for t in texts]
    old = None if force else load_vectors(folder)
    known: dict[tuple[str, str], np.ndarray] = {}
    if old is not None and old.model == embedder.name:
        known = {
            (i, fp): old.vectors[n]
            for n, (i, fp) in enumerate(zip(old.ids, old.fingerprints, strict=True))
        }
    missing = [n for n in range(len(ids)) if (ids[n], prints[n]) not in known]
    fresh = embedder.embed([texts[n] for n in missing]) if missing else np.zeros((0, 0))
    fresh_by_pos = {n: fresh[k] for k, n in enumerate(missing)}
    vectors = np.vstack(
        [
            fresh_by_pos[n] if n in fresh_by_pos else known[(ids[n], prints[n])]
            for n in range(len(ids))
        ]
    ).astype(np.float32)
    save_vectors(VectorIndex(embedder.name, ids, prints, vectors), folder)
    save_bm25(build_bm25(ids, texts), folder)
    return {
        "isin": isin,
        "chunks": len(ids),
        "embedded": len(missing),
        "reused": len(ids) - len(missing),
    }
