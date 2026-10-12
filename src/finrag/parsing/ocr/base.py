"""The OCR interface and its result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class OcrUnavailable(Exception):
    """Raised when a backend cannot run (not installed, no model, bad response); says why."""


@dataclass(frozen=True)
class OcrWord:
    """One recognised word with its box in points on the rendered (displayed) page."""

    text: str
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass(frozen=True)
class OcrResult:
    """What a backend read from one page image.

    `words` carry positions (Tesseract); `tables` are cell matrices a backend found itself
    (Chandra, once its response parser exists). Either may be empty.
    """

    text: str
    tables: tuple[tuple[tuple[str, ...], ...], ...]
    backend: str
    words: tuple[OcrWord, ...] = ()


class OcrBackend(Protocol):
    """Reads a rendered page image (PNG bytes) into text, words and tables."""

    @property
    def name(self) -> str:
        """Backend name as stored in parsed_facts.ocr_backend."""
        ...

    def ocr_page(self, image: bytes) -> OcrResult:
        """Page image bytes -> OcrResult; raises OcrUnavailable if the backend cannot run."""
        ...
