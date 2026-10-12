"""Chandra backend: a vision model behind an OpenAI-compatible endpoint (LM Studio).

Built: image downscale, request, error handling. NOT built: the response parser. The prompt and
the layout of the model's answer have not been seen yet, so nothing here guesses them.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Callable
from typing import Any

from finrag.core.config import Settings, get_settings
from finrag.parsing.ocr.base import OcrResult, OcrUnavailable

NAME = "chandra"
# TODO(M03): the real prompt for the Chandra model. Until it is confirmed against the model, no
# text part is sent, only the image.
PROMPT = ""

PostFn = Callable[[str, dict[str, Any], float], dict[str, Any]]


def _httpx_post(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    """POST JSON with httpx -> decoded JSON body; transport and HTTP errors raise OcrUnavailable."""
    import httpx

    try:
        response = httpx.post(url, json=body, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OcrUnavailable(f"chandra endpoint failed: {exc}") from exc


def downscale(image: bytes, max_px: int) -> bytes:
    """PNG bytes -> PNG bytes whose longest side is at most max_px (unchanged if already small)."""
    from PIL import Image

    picture = Image.open(io.BytesIO(image))
    longest = max(picture.size)
    if longest <= max_px:
        return image
    factor = max_px / longest
    size = (max(1, round(picture.width * factor)), max(1, round(picture.height * factor)))
    out = io.BytesIO()
    picture.resize(size, Image.Resampling.LANCZOS).save(out, format="PNG")
    return out.getvalue()


class ChandraBackend:
    """Send a page image to the vision endpoint; the answer cannot be read yet (see module doc)."""

    name = NAME

    def __init__(self, settings: Settings | None = None, post: PostFn | None = None) -> None:
        """`post` is the HTTP call (injectable for tests); httpx by default."""
        self._cfg = (settings or get_settings()).parsing.ocr.chandra
        self._post = post or _httpx_post

    def request_body(self, image: bytes) -> dict[str, Any]:
        """Page image -> chat-completions payload with the image as a data-URL content part."""
        encoded = base64.b64encode(downscale(image, self._cfg.max_image_px)).decode("ascii")
        parts: list[dict[str, Any]] = []
        if PROMPT:
            parts.append({"type": "text", "text": PROMPT})
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
        )
        return {"model": self._cfg.model, "messages": [{"role": "user", "content": parts}]}

    def ocr_page(self, image: bytes) -> OcrResult:
        """Page image -> OcrResult; raises OcrUnavailable (no model, endpoint down, parser TODO)."""
        if not self._cfg.model:
            raise OcrUnavailable("parsing.ocr.chandra.model is blank: load a model and set it")
        url = f"{self._cfg.base_url.rstrip('/')}/chat/completions"
        self._post(url, self.request_body(image), float(self._cfg.timeout_s))
        raise OcrUnavailable(
            "TODO(M03): the Chandra response parser is not built; a sample model response is "
            "needed first"
        )
