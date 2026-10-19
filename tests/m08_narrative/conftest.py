"""Fixtures for M08: synthetic narrative inputs, filings in a temp database, a mock embedder."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest
import sqlalchemy as sa

from finrag.core.config import get_settings
from finrag.core.db import init_db, tables, transaction
from finrag.core.ids import filing_id_from_sha
from finrag.core.types import utc_now_iso

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
ISIN_A = "INE111A01010"
ISIN_B = "INE222B01028"


def load_fixture(name: str) -> ModuleType:
    """Import tests/fixtures/<name>.py (tests cannot import from other test folders)."""
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def registries(isolated_repo: Path) -> None:
    """The temp repo gets the real sections registry (the global fixture copies settings only)."""
    shutil.copy(REPO / "config" / "sections.yaml", isolated_repo / "config" / "sections.yaml")


@pytest.fixture
def fx() -> ModuleType:
    """The synthetic narrative inputs."""
    return load_fixture("make_narrative")


class MockEmbedder:
    """Returns a deterministic random unit vector per text (so only BM25 can find a term)."""

    name = "mock-embedder"

    def __init__(self, dim: int = 16) -> None:
        self.dim = dim
        self.calls: list[int] = []

    def _vector(self, text: str) -> np.ndarray:
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        vec = np.random.default_rng(seed).normal(size=self.dim)
        return (vec / np.linalg.norm(vec)).astype(np.float32)

    def embed(self, texts: list[str]) -> np.ndarray:
        self.calls.append(len(texts))
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._vector(t) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._vector(text)


@pytest.fixture
def embedder() -> MockEmbedder:
    """A mock embedder (random vectors)."""
    return MockEmbedder()


@pytest.fixture
def db() -> None:
    """A migrated temp database holding two companies."""
    init_db()
    with transaction() as conn:
        for isin, name in ((ISIN_A, "Acme Widgets"), (ISIN_B, "Bolt Banking")):
            conn.execute(
                sa.insert(tables.entities).values(
                    isin=isin, name=name, status="active", added_on="2026-10-01"
                )
            )


PERIODS = {
    "Q4FY26": (date(2026, 1, 1), date(2026, 3, 31), "Q"),
    "Q1FY27": (date(2026, 4, 1), date(2026, 6, 30), "Q"),
}


def _add_filing(
    isin: str,
    filing_type: str,
    fmt: str,
    period: str = "Q4FY26",
    basis: str = "standalone",
    submission: str = "original",
    revision_no: int = 0,
    filed_on: str = "2026-05-05",
    status: str = "parsed",
    pages: list[dict[str, Any]] | None = None,
    text_facts: list[dict[str, Any]] | None = None,
    key: str = "",
) -> str:
    """Register a filing row and write its processed files -> filing_id."""
    start, end, ptype = PERIODS[period]
    sha = hashlib.sha256(f"{isin}{filing_type}{fmt}{period}{basis}{revision_no}{key}".encode())
    filing_id = filing_id_from_sha(sha.hexdigest())
    with transaction() as conn:
        conn.execute(
            sa.insert(tables.filings).values(
                filing_id=filing_id,
                isin=isin,
                source="test",
                filing_type=filing_type,
                submission=submission,
                revision_no=revision_no,
                taxonomy_family="indas",
                basis=basis,
                period_start=start.isoformat(),
                period_end=end.isoformat(),
                period_type=ptype,
                filed_on=filed_on,
                raw_path="x",
                sha256=sha.hexdigest(),
                format=fmt,
                status=status,
                created_at=utc_now_iso(),
            )
        )
    folder = get_settings().paths.processed / filing_id
    folder.mkdir(parents=True, exist_ok=True)
    if pages is not None:
        (folder / "pages.jsonl").write_text(
            "\n".join(json.dumps(p) for p in pages) + "\n", encoding="utf-8"
        )
    if text_facts is not None:
        (folder / "text_facts.jsonl").write_text(
            "\n".join(json.dumps(f) for f in text_facts) + "\n", encoding="utf-8"
        )
    return filing_id


def _chunk_rows(filing_id: str | None = None) -> list[sa.Row]:
    """All chunk rows (of one filing), in seq order."""
    c = tables.chunks
    query = sa.select(c).order_by(c.c.filing_id, c.c.seq)
    if filing_id:
        query = query.where(c.c.filing_id == filing_id)
    with transaction() as conn:
        return list(conn.execute(query))


@pytest.fixture
def add_filing() -> Any:
    """Function: register a filing and write its processed files."""
    return _add_filing


@pytest.fixture
def chunk_rows() -> Any:
    """Function: chunk rows of the database (optionally of one filing)."""
    return _chunk_rows


@pytest.fixture
def corpus(db: None, fx: ModuleType, embedder: MockEmbedder) -> dict[str, str]:
    """Two companies, four periods of narrative, indexed with the mock embedder.

    Company A: transcripts for Q4 FY26 and Q1 FY27 (same call text, so only the filter can tell
    them apart), an XBRL filing with a revision, a results PDF, an annual report (deferred).
    Company B: a Q4 FY26 transcript with the same text as A's.
    """
    from finrag.narrative.runner import run_index

    ids = {
        "a_q4": _add_filing(
            ISIN_A,
            "earnings_call_transcript",
            "pdf",
            "Q4FY26",
            pages=fx.transcript_pages(),
            key="t",
        ),
        "a_q1": _add_filing(
            ISIN_A,
            "earnings_call_transcript",
            "pdf",
            "Q1FY27",
            pages=fx.transcript_pages(),
            key="t",
        ),
        "b_q4": _add_filing(
            ISIN_B,
            "earnings_call_transcript",
            "pdf",
            "Q4FY26",
            pages=fx.transcript_pages(),
            key="t",
        ),
        "a_xbrl": _add_filing(
            ISIN_A, "financial_results", "xbrl", "Q4FY26", text_facts=fx.text_facts()
        ),
        "a_pdf": _add_filing(
            ISIN_A, "financial_results", "pdf", "Q4FY26", pages=fx.results_pages()
        ),
        "a_ar": _add_filing(ISIN_A, "annual_report", "pdf", "Q4FY26", pages=fx.results_pages()),
    }
    run_index(embedder=embedder)
    return ids
