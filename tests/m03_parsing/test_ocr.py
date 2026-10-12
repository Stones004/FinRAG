"""OCR backends: Tesseract (fake engine), Chandra (fake HTTP) and the factory. No real OCR."""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image

from finrag.core.config import get_settings, reset_settings_cache
from finrag.parsing.ocr import chandra
from finrag.parsing.ocr.base import OcrUnavailable
from finrag.parsing.ocr.chandra import ChandraBackend, downscale
from finrag.parsing.ocr.factory import get_backend
from finrag.parsing.ocr.tesseract import TesseractBackend


def png(width: int, height: int) -> bytes:
    """A blank PNG of the given size."""
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


def set_ocr(isolated_repo, **changes) -> None:
    """Edit parsing.ocr values in the temp repo's settings.yaml and reload."""
    path = isolated_repo / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        node = data["parsing"]["ocr"]
        *parents, leaf = key.split(".")
        for part in parents:
            node = node[part]
        node[leaf] = value
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    reset_settings_cache()


class FakeEngine:
    """Stands in for the pytesseract module."""

    Output = SimpleNamespace(DICT="dict")

    def __init__(self, data=None, error: Exception | None = None) -> None:
        self.data, self.error, self.calls = data, error, []
        self.pytesseract = SimpleNamespace(tesseract_cmd="tesseract")

    def image_to_data(self, image, lang, config, output_type):
        self.calls.append(
            {"size": image.size, "lang": lang, "config": config, "output_type": output_type}
        )
        if self.error:
            raise self.error
        return self.data


def data_for(*words):
    """image_to_data dict for (text, left, top, width, height, line) words, in pixels."""
    keys = ("text", "left", "top", "width", "height", "line_num")
    columns = {k: [w[i] for w in words] for i, k in enumerate(keys)}
    columns["conf"] = [90.0] * len(words)
    columns["block_num"] = [1] * len(words)
    columns["par_num"] = [1] * len(words)
    return columns


def test_tesseract_words_are_scaled_from_pixels_to_points() -> None:
    dpi = get_settings().parsing.ocr.ocr_dpi
    data = data_for(("Revenue", 300, 600, 600, 90, 1), ("7,737.02", 1500, 600, 300, 90, 1))
    result = TesseractBackend(engine=FakeEngine(data)).ocr_page(png(40, 40))
    scale = 72 / dpi
    assert result.backend == "tesseract"
    assert result.text == "Revenue 7,737.02"
    first = result.words[0]
    assert (first.text, first.x0, first.x1) == ("Revenue", 300 * scale, 900 * scale)
    assert (first.top, first.bottom) == (600 * scale, 690 * scale)


def test_tesseract_skips_layout_rows_and_empty_words() -> None:
    data = data_for(("", 0, 0, 100, 100, 0), ("Total", 10, 10, 50, 20, 1), ("  ", 5, 5, 5, 5, 1))
    data["conf"] = [-1.0, 80.0, 70.0]
    result = TesseractBackend(engine=FakeEngine(data)).ocr_page(png(10, 10))
    assert [w.text for w in result.words] == ["Total"]


def test_tesseract_uses_configured_language_and_command(isolated_repo) -> None:
    set_ocr(isolated_repo, tesseract_cmd="C:/tools/tesseract.exe", ocr_lang="eng+hin")
    engine = FakeEngine(data_for(("a", 0, 0, 1, 1, 1)))
    TesseractBackend(engine=engine).ocr_page(png(10, 10))
    assert engine.calls[0]["lang"] == "eng+hin"
    assert [c["config"] for c in engine.calls] == get_settings().parsing.ocr.tesseract_passes
    assert engine.pytesseract.tesseract_cmd == "C:/tools/tesseract.exe"


def test_tesseract_blank_cmd_leaves_the_path_lookup_alone() -> None:
    engine = FakeEngine(data_for(("a", 0, 0, 1, 1, 1)))
    TesseractBackend(engine=engine).ocr_page(png(10, 10))
    assert engine.pytesseract.tesseract_cmd == "tesseract"


def test_tesseract_failure_is_ocr_unavailable() -> None:
    engine = FakeEngine(error=RuntimeError("tesseract is not installed"))
    with pytest.raises(OcrUnavailable, match="not installed"):
        TesseractBackend(engine=engine).ocr_page(png(10, 10))


def test_chandra_downscales_to_max_image_px() -> None:
    big = png(3000, 1500)
    small = Image.open(io.BytesIO(downscale(big, 2000)))
    assert max(small.size) == 2000
    assert small.size == (2000, 1000)  # aspect ratio kept
    untouched = png(500, 400)
    assert downscale(untouched, 2000) == untouched


def test_chandra_request_carries_the_image_as_a_data_url(isolated_repo) -> None:
    set_ocr(isolated_repo, **{"chandra.model": "chandra-ocr", "chandra.max_image_px": 1000})
    body = ChandraBackend().request_body(png(3000, 1500))
    assert body["model"] == "chandra-ocr"
    part = body["messages"][0]["content"][-1]
    assert part["type"] == "image_url"
    prefix = "data:image/png;base64,"
    assert part["image_url"]["url"].startswith(prefix)
    sent = Image.open(io.BytesIO(base64.b64decode(part["image_url"]["url"][len(prefix) :])))
    assert max(sent.size) <= 1000


def test_chandra_blank_model_is_unavailable() -> None:
    with pytest.raises(OcrUnavailable, match="model is blank"):
        ChandraBackend(post=lambda *a: pytest.fail("must not call the endpoint")).ocr_page(
            png(10, 10)
        )


def test_chandra_endpoint_error_is_unavailable(isolated_repo) -> None:
    set_ocr(isolated_repo, **{"chandra.model": "chandra-ocr"})

    def down(url, body, timeout):
        raise OcrUnavailable("chandra endpoint failed: connection refused")

    with pytest.raises(OcrUnavailable, match="connection refused"):
        ChandraBackend(post=down).ocr_page(png(10, 10))


def test_chandra_posts_to_the_configured_endpoint_then_reports_the_parser_todo(
    isolated_repo,
) -> None:
    set_ocr(isolated_repo, **{"chandra.model": "chandra-ocr", "chandra.timeout_s": 7})
    seen = {}

    def post(url, body, timeout):
        seen.update(url=url, timeout=timeout)
        return {"choices": []}

    with pytest.raises(OcrUnavailable, match="TODO"):
        ChandraBackend(post=post).ocr_page(png(10, 10))
    assert seen == {"url": "http://localhost:1234/v1/chat/completions", "timeout": 7.0}


def test_factory_follows_the_configured_backend(isolated_repo) -> None:
    assert get_backend().name == "tesseract"
    set_ocr(isolated_repo, backend="chandra")
    assert get_backend().name == chandra.NAME == "chandra"


def test_later_passes_only_add_words_that_overlap_nothing() -> None:
    first = data_for(("Total", 100, 100, 200, 90, 1), ("7,839.37", 1500, 100, 300, 90, 1))
    second = data_for(
        ("Total", 110, 105, 200, 90, 1),  # same word, found again: dropped
        ("8,115.60", 2000, 100, 300, 90, 1),  # a cell the first pass missed: added
        ("junk", 100, 600, 100, 90, 1),
    )

    class TwoPass(FakeEngine):
        def image_to_data(self, image, lang, config, output_type):
            super().image_to_data(image, lang, config, output_type)
            return first if config == "--psm 3" else second

    result = TesseractBackend(engine=TwoPass()).ocr_page(png(10, 10))
    assert [w.text for w in result.words] == ["Total", "7,839.37", "8,115.60", "junk"]
    assert result.text == "Total 7,839.37"  # page text comes from the first pass


def test_remove_rules_erases_long_lines_and_keeps_short_strokes() -> None:
    from PIL import ImageDraw

    from finrag.parsing.ocr.tesseract import remove_rules

    image = Image.new("L", (400, 300), 255)
    draw = ImageDraw.Draw(image)
    draw.line([(20, 50), (380, 50)], fill=0, width=2)  # long horizontal rule
    draw.line([(100, 20), (100, 280)], fill=0, width=2)  # long vertical rule
    draw.rectangle([200, 120, 210, 140], fill=0)  # a letter-sized blob
    cleaned = remove_rules(image, 100)
    assert cleaned.getpixel((250, 50)) == 255
    assert cleaned.getpixel((100, 200)) == 255
    assert cleaned.getpixel((205, 130)) == 0
