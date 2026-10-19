"""Text embeddings: a small interface and the local sentence-transformers implementation."""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np

from finrag.core.config import NarrativeSettings


class Embedder(Protocol):
    """Anything that turns texts into unit-length vectors (mockable in tests)."""

    @property
    def name(self) -> str:
        """Identifier stored with the vectors; a change means every vector is re-computed."""
        ...

    def embed(self, texts: list[str]) -> np.ndarray:
        """Texts -> float32 array (n, dim), rows normalised to length 1."""
        ...

    def embed_query(self, text: str) -> np.ndarray:
        """Search query -> one normalised vector (the model's query instruction is applied)."""
        ...


class SentenceTransformerEmbedder:
    """The model named in narrative.embedding_model, run locally on narrative.embedding_device."""

    def __init__(self, settings: NarrativeSettings) -> None:
        self._settings = settings
        self._model: Any = None

    @property
    def name(self) -> str:
        """The configured model name."""
        return self._settings.embedding_model

    def _load(self) -> Any:
        """Load the model once (the first call downloads it if it is not cached locally)."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self._settings.embedding_model, device=self._settings.embedding_device
            )
        return self._model

    def embed(self, texts: list[str]) -> np.ndarray:
        """Texts -> normalised float32 vectors, in batches of embedding_batch_size."""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vectors = self._load().encode(
            texts,
            batch_size=self._settings.embedding_batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        """Query -> one vector, with the model's query prefix from settings."""
        return self.embed([self._settings.embedding_query_prefix + text])[0]
