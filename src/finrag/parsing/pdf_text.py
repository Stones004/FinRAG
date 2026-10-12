"""Page text from a PDF: native text layer first, OCR for pages that are empty or unreadable."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from finrag.core.config import Settings, get_settings
from finrag.core.logging import get_logger
from finrag.parsing.ocr.base import OcrBackend, OcrUnavailable, OcrWord
from finrag.parsing.text_quality import Vocabulary, text_quality

METHOD_NATIVE = "native"
METHOD_OCR = "ocr"
FILING_TYPE_TRANSCRIPT = "earnings_call_transcript"
REASON_TOO_SHORT = "too_short"
REASON_LOW_QUALITY = "low_quality"
_BOLD_FLAG = 16  # PyMuPDF span flag bit for bold

log = get_logger("parsing")


class PdfReadError(Exception):
    """Raised when the bytes are not a readable PDF."""


@dataclass(frozen=True)
class PageText:
    """One page's text and how it was obtained."""

    page: int  # 1-based
    text: str
    method: str  # native | ocr
    ocr_backend: str | None
    char_count: int
    quality: float  # share of recognisable tokens in `text`
    low_quality: bool  # text is non-blank but scores below parsing.pdf.min_text_quality
    ocr_reason: str | None = None  # why OCR was tried: too_short | low_quality
    ocr_error: str | None = None  # OCR was tried and could not run
    native_quality: float | None = None  # score of the native layer when OCR replaced it
    lines: list[dict[str, Any]] | None = None  # transcripts: [{text, bold, font, size}]
    words: tuple[OcrWord, ...] = ()  # OCR pages: positioned words (points)

    def as_json(self) -> dict[str, Any]:
        """PageText -> one pages.jsonl record."""
        record: dict[str, Any] = {
            "page": self.page,
            "text": self.text,
            "method": self.method,
            "ocr_backend": self.ocr_backend,
            "char_count": self.char_count,
            "lines": self.lines,
            "quality": round(self.quality, 4),
            "low_quality": self.low_quality,
        }
        for key in ("ocr_reason", "ocr_error", "native_quality"):
            if getattr(self, key) is not None:
                record[key] = getattr(self, key)
        if self.words:
            record["words"] = [
                [w.text, round(w.x0, 2), round(w.x1, 2), round(w.top, 2), round(w.bottom, 2)]
                for w in self.words
            ]
        return record


@dataclass
class PdfTextResult:
    """All pages of one PDF plus counts for the job stats."""

    pages: list[PageText] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        """Counts and page lists for the jobs row (low-quality and OCR-failed pages by number)."""
        return {
            "pages": len(self.pages),
            "native_pages": sum(p.method == METHOD_NATIVE for p in self.pages),
            "ocr_pages": sum(p.method == METHOD_OCR for p in self.pages),
            "blank_pages": sum(p.char_count == 0 for p in self.pages),
            "low_quality_pages": [p.page for p in self.pages if p.low_quality],
            "ocr_failed_pages": [p.page for p in self.pages if p.ocr_error],
        }


def _lines_of(page: Any) -> list[dict[str, Any]]:
    """A PyMuPDF page -> [{text, bold, font, size}] per text line (bold = first span is bold)."""
    lines: list[dict[str, Any]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            first = spans[0]
            lines.append(
                {
                    "text": "".join(s["text"] for s in line["spans"]).strip(),
                    "bold": bool(first["flags"] & _BOLD_FLAG) or "bold" in first["font"].lower(),
                    "font": first["font"],
                    "size": round(first["size"], 2),
                }
            )
    return lines


def _render_png(page: Any, dpi: int) -> bytes:
    """A PyMuPDF page -> PNG bytes as displayed (rotation applied) at the given DPI."""
    return page.get_pixmap(dpi=dpi).tobytes("png")


def extract_pdf_text(
    data: bytes,
    filing_type: str,
    backend: OcrBackend | None,
    vocabulary: Vocabulary,
    settings: Settings | None = None,
) -> PdfTextResult:
    """PDF bytes -> PageText per page; OCR (if enabled and a backend is given) for bad pages.

    A page is sent to OCR when its native text is shorter than min_text_chars_for_native or
    scores below min_text_quality. The better-scoring text is kept; a non-blank page that still
    scores below the threshold is flagged `low_quality`, never silently accepted.
    """
    import pymupdf

    settings = settings or get_settings()
    pdf, ocr = settings.parsing.pdf, settings.parsing.ocr
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # pymupdf raises several error types for bad files
        raise PdfReadError(f"cannot read PDF: {exc}") from exc
    result = PdfTextResult()
    ocr_down: str | None = None  # first reason OCR could not run; later pages are not retried
    with doc:
        for index, page in enumerate(doc):
            native = page.get_text()
            native_quality = text_quality(native, vocabulary)
            too_short = len(native.strip()) < pdf.min_text_chars_for_native
            reason = (
                REASON_TOO_SHORT
                if too_short
                else REASON_LOW_QUALITY
                if native_quality < pdf.min_text_quality
                else None
            )
            text, method, backend_name, quality = native, METHOD_NATIVE, None, native_quality
            words: tuple[OcrWord, ...] = ()
            error: str | None = None
            if reason and ocr.ocr_enabled and backend is not None:
                if ocr_down is None:
                    try:
                        read = backend.ocr_page(_render_png(page, ocr.ocr_dpi))
                        read_quality = text_quality(read.text, vocabulary)
                        if read_quality > native_quality:
                            text, method, quality = read.text, METHOD_OCR, read_quality
                            backend_name, words = read.backend, read.words
                    except OcrUnavailable as exc:
                        ocr_down = str(exc)
                        log.warning("OCR unavailable: %s", exc, extra={"event": "ocr_unavailable"})
                if ocr_down is not None:
                    error = ocr_down
            elif reason and not (ocr.ocr_enabled and backend is not None):
                error = "OCR disabled or no backend" if not ocr.ocr_enabled else "no OCR backend"
            blank = not text.strip()
            result.pages.append(
                PageText(
                    page=index + 1,
                    text=text,
                    method=method,
                    ocr_backend=backend_name,
                    char_count=len(text.strip()),
                    quality=quality,
                    low_quality=not blank and quality < pdf.min_text_quality,
                    ocr_reason=reason,
                    ocr_error=error,
                    native_quality=native_quality if method == METHOD_OCR else None,
                    lines=_lines_of(page)
                    if filing_type == FILING_TYPE_TRANSCRIPT and method == METHOD_NATIVE
                    else None,
                    words=words,
                )
            )
    return result


def write_pages_jsonl(path: Path, pages: Iterable[PageText]) -> None:
    """Write one JSON line per page (creates the folder; replaces an earlier file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for page in pages:
            handle.write(json.dumps(page.as_json(), ensure_ascii=False) + "\n")


def read_pages_jsonl(path: Path) -> list[dict[str, Any]]:
    """pages.jsonl -> list of page records."""
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
