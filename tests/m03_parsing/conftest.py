"""Fixtures for M03: synthetic XBRL and PDFs, registered filings in a temp repo, fake OCR."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pymupdf
import pytest
import sqlalchemy as sa

from finrag.core import config
from finrag.core.db import init_db, tables, transaction
from finrag.parsing.ocr.base import OcrResult, OcrUnavailable, OcrWord

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
ISIN = "INE111A01010"


def _load(name: str) -> ModuleType:
    """Import tests/fixtures/<name>.py (tests cannot import from other test folders)."""
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def registries(isolated_repo: Path) -> None:
    """The temp repo gets the real sections registry and word list (global fixture copies only
    settings.yaml)."""
    shutil.copy(REPO / "config" / "sections.yaml", isolated_repo / "config" / "sections.yaml")
    (isolated_repo / "config" / "vocab").mkdir()
    shutil.copy(
        REPO / "config" / "vocab" / "common_en.txt",
        isolated_repo / "config" / "vocab" / "common_en.txt",
    )


def text_pdf(pages: list[str], font_size: int = 10) -> bytes:
    """A PDF with one page of plain text per string (one line per \\n)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        y = 60
        for line in [w for raw in text.split("\n") for w in textwrap.wrap(raw, 80) or [""]]:
            page.insert_text((50, y), line, fontsize=font_size)
            y += font_size + 4
    data = doc.tobytes()
    doc.close()
    return data


def blank_pdf(pages: int = 1) -> bytes:
    """A PDF with empty pages (no text layer at all)."""
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


class FakeOcr:
    """An OcrBackend that returns canned results by call order and records the images it got."""

    def __init__(self, results: list[OcrResult | Exception], name: str = "tesseract") -> None:
        self.results = list(results)
        self.images: list[bytes] = []
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def ocr_page(self, image: bytes) -> OcrResult:
        self.images.append(image)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def ocr_result(text: str, words: tuple[OcrWord, ...] = (), backend: str = "tesseract") -> OcrResult:
    """A canned OCR result."""
    return OcrResult(text=text, tables=(), backend=backend, words=words)


@pytest.fixture
def entity() -> str:
    """An initialised database holding one active entity; returns its ISIN."""
    init_db()
    with transaction() as conn:
        conn.execute(
            sa.insert(tables.entities).values(
                isin=ISIN,
                name="Alpha Industries Limited",
                nse_symbol="ALPHAX",
                bse_code="500001",
                sector_type="general",
                default_basis="consolidated",
                status="active",
                added_on="2026-10-12",
                updated_on="2026-10-12",
            )
        )
    return ISIN


@pytest.fixture
def register(entity: str, isolated_repo: Path):
    """register(data, fmt, filing_type, ...) -> filing_id: store the bytes and add a filings row."""

    def _register(
        data: bytes,
        fmt: str = "xbrl",
        filing_type: str = "financial_results",
        basis: str = "standalone",
        status: str = "fetched",
        name: str | None = None,
    ) -> str:
        sha = hashlib.sha256(data).hexdigest()
        filing_id = f"FIL-{sha[:12]}"
        raw = (
            isolated_repo
            / "data"
            / "raw"
            / ISIN
            / f"{name or filing_id}.{'xml' if fmt == 'xbrl' else 'pdf'}"
        )
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(data)
        with transaction() as conn:
            conn.execute(
                sa.insert(tables.filings).values(
                    filing_id=filing_id,
                    isin=ISIN,
                    source="local_inbox",
                    filing_type=filing_type,
                    submission="original",
                    revision_no=0,
                    taxonomy_family="indas",
                    basis=basis,
                    period_start="2026-01-01",
                    period_end="2026-03-31",
                    period_type="Q",
                    raw_path=str(raw),
                    sha256=sha,
                    format=fmt,
                    status=status,
                    created_at="2026-10-12T00:00:00+00:00",
                )
            )
        return filing_id

    return _register


@pytest.fixture
def kit() -> SimpleNamespace:
    """Builders and constants (tests cannot import from conftest in importlib mode)."""
    xbrl, results = _load("make_xbrl"), _load("make_results_pdf")
    return SimpleNamespace(
        ISIN=ISIN,
        xbrl=xbrl,
        results=results,
        text_pdf=text_pdf,
        blank_pdf=blank_pdf,
        FakeOcr=FakeOcr,
        ocr_result=ocr_result,
        OcrUnavailable=OcrUnavailable,
        OcrWord=OcrWord,
        settings=config.get_settings,
    )
