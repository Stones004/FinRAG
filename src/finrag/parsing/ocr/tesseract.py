"""Tesseract backend: pytesseract on a page rendered at parsing.ocr.ocr_dpi."""

from __future__ import annotations

import io
from collections import defaultdict
from typing import Any

from finrag.core.config import Settings, get_settings
from finrag.parsing.ocr.base import OcrResult, OcrUnavailable, OcrWord

NAME = "tesseract"
_POINTS_PER_INCH = 72
_MIN_SHARED_FRACTION = 0.3  # share of the narrower word that must overlap to be the same word
_DARK_BELOW = 140  # grey levels below this count as ink when looking for rules
_RULE_GROW = 3  # pixels (odd) the rule mask is widened by so anti-aliased edges go too
_NO_CONFIDENCE = -1  # rows of image_to_data that are layout levels, not words


class TesseractBackend:
    """Run Tesseract; word boxes are converted from pixels to points (the render DPI)."""

    name = NAME

    def __init__(self, settings: Settings | None = None, engine: Any | None = None) -> None:
        """`engine` is the pytesseract module (injectable for tests); imported lazily if None."""
        self._ocr = (settings or get_settings()).parsing.ocr
        self._engine = engine

    def _tesseract(self) -> Any:
        """Return the pytesseract module, configured with tesseract_cmd, or raise OcrUnavailable."""
        if self._engine is None:
            try:
                import pytesseract
            except ImportError as exc:
                raise OcrUnavailable("pytesseract is not installed") from exc
            self._engine = pytesseract
        if self._ocr.tesseract_cmd:
            self._engine.pytesseract.tesseract_cmd = self._ocr.tesseract_cmd
        return self._engine

    def ocr_page(self, image: bytes) -> OcrResult:
        """PNG bytes -> OcrResult: text from the first pass, words merged over all passes."""
        from PIL import Image

        engine = self._tesseract()
        try:
            picture = Image.open(io.BytesIO(image))
            min_rule = round(self._ocr.rule_min_length_pt * self._ocr.ocr_dpi / _POINTS_PER_INCH)
            if min_rule:
                picture = remove_rules(picture, min_rule)
            results = [
                _result_from_data(
                    engine.image_to_data(
                        picture,
                        lang=self._ocr.ocr_lang,
                        config=options,
                        output_type=engine.Output.DICT,
                    ),
                    _POINTS_PER_INCH / self._ocr.ocr_dpi,
                )
                for options in self._ocr.tesseract_passes
            ]
        except Exception as exc:  # pytesseract raises several error types
            raise OcrUnavailable(f"tesseract failed: {exc}") from exc
        words = results[0].words
        for later in results[1:]:
            words = _merge(words, later.words)
        return OcrResult(text=results[0].text, tables=(), backend=NAME, words=words)


def _long_runs(dark: Any, length: int, dx: int, dy: int) -> Any:
    """Mask image -> mask of pixels lying on a straight dark run of at least `length` pixels."""
    from PIL import ImageChops

    run, step = dark, 1
    while step < length:  # keep a pixel only if the pixel `step` back is dark too: runs double
        run = ImageChops.darker(run, ImageChops.offset(run, dx * step, dy * step))
        step *= 2
    grown, step = run, 1
    while step < length:  # spread the run ends back over the whole run
        grown = ImageChops.lighter(grown, ImageChops.offset(grown, -dx * step, -dy * step))
        step *= 2
    return grown


def remove_rules(image: Any, min_length: int) -> Any:
    """Page image -> grey image with horizontal and vertical lines of >= min_length px erased."""
    from PIL import ImageChops, ImageFilter

    gray = image.convert("L")
    dark = gray.point(lambda v: 255 if v < _DARK_BELOW else 0)
    rules = ImageChops.lighter(
        _long_runs(dark, min_length, 1, 0), _long_runs(dark, min_length, 0, 1)
    )
    return ImageChops.lighter(gray, rules.filter(ImageFilter.MaxFilter(_RULE_GROW)))


def _overlaps(a: OcrWord, b: OcrWord) -> bool:
    """True if two word boxes cover the same spot (shared width and vertical centre inside)."""
    shared = min(a.x1, b.x1) - max(a.x0, b.x0)
    if shared <= _MIN_SHARED_FRACTION * min(a.x1 - a.x0, b.x1 - b.x0):
        return False
    centre = (b.top + b.bottom) / 2
    return a.top <= centre <= a.bottom


def _merge(base: tuple[OcrWord, ...], extra: tuple[OcrWord, ...]) -> tuple[OcrWord, ...]:
    """Words of `extra` that overlap no word of `base`, added to `base`."""
    return base + tuple(w for w in extra if not any(_overlaps(b, w) for b in base))


def _result_from_data(data: dict[str, list[Any]], to_points: float) -> OcrResult:
    """image_to_data dict -> OcrResult (pixel boxes scaled to points)."""
    words: list[OcrWord] = []
    lines: dict[tuple[int, int, int], list[str]] = defaultdict(list)
    for i, raw in enumerate(data["text"]):
        text = str(raw).strip()
        if not text or float(data["conf"][i]) == _NO_CONFIDENCE:
            continue
        left, top = float(data["left"][i]), float(data["top"][i])
        words.append(
            OcrWord(
                text,
                left * to_points,
                (left + float(data["width"][i])) * to_points,
                top * to_points,
                (top + float(data["height"][i])) * to_points,
            )
        )
        lines[(data["block_num"][i], data["par_num"][i], data["line_num"][i])].append(text)
    text = "\n".join(" ".join(parts) for parts in lines.values())
    return OcrResult(text=text, tables=(), backend=NAME, words=tuple(words))
