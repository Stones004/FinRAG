"""Pick the OCR backend named by parsing.ocr.backend."""

from __future__ import annotations

from finrag.core.config import ConfigError, Settings, get_settings
from finrag.parsing.ocr.base import OcrBackend
from finrag.parsing.ocr.chandra import ChandraBackend
from finrag.parsing.ocr.tesseract import TesseractBackend


def get_backend(settings: Settings | None = None) -> OcrBackend:
    """settings.parsing.ocr.backend -> a backend instance."""
    settings = settings or get_settings()
    name = settings.parsing.ocr.backend
    if name == "tesseract":
        return TesseractBackend(settings)
    if name == "chandra":
        return ChandraBackend(settings)
    raise ConfigError(f"parsing.ocr.backend: unknown backend '{name}'")
