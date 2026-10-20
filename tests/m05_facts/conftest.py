"""Fixtures for M05: a temp database, filings, parsed facts and candidates."""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
import yaml

from finrag.core.db import init_db, tables, transaction
from finrag.facts.writer import write_candidates
from finrag.normalize.models import CanonicalCandidate
from finrag.normalize.taxonomy import Taxonomy, parse_taxonomy

ISIN = "INE111A01010"
REAL = Path(__file__).resolve().parents[2]
D = Decimal
FY26 = {
    "Q1": (date(2025, 4, 1), date(2025, 6, 30), "Q"),
    "Q2": (date(2025, 7, 1), date(2025, 9, 30), "Q"),
    "Q3": (date(2025, 10, 1), date(2025, 12, 31), "Q"),
    "Q4": (date(2026, 1, 1), date(2026, 3, 31), "Q"),
    "H1": (date(2025, 4, 1), date(2025, 9, 30), "H"),
    "H2": (date(2025, 10, 1), date(2026, 3, 31), "H"),
    "9M": (date(2025, 4, 1), date(2025, 12, 31), "9M"),
    "FY": (date(2025, 4, 1), date(2026, 3, 31), "FY"),
}


@pytest.fixture(scope="session")
def real_taxonomy() -> Taxonomy:
    """The real taxonomy registry."""
    return parse_taxonomy(yaml.safe_load((REAL / "config" / "taxonomy.yaml").read_text("utf-8")))


@pytest.fixture(autouse=True)
def real_taxonomy_file(isolated_repo: Path) -> None:
    """Put the real taxonomy where settings.paths.taxonomy points (derivation loads it)."""
    shutil.copy(REAL / "config" / "taxonomy.yaml", isolated_repo / "config" / "taxonomy.yaml")


@pytest.fixture
def db() -> None:
    """Temp database with one entity."""
    init_db()
    with transaction() as conn:
        conn.execute(
            sa.insert(tables.entities).values(
                isin=ISIN,
                name="X",
                sector_type="general",
                default_basis="consolidated",
                status="active",
            )  # fmt: skip
        )


def add_filing(
    filing_id: str,
    filed_on: str | None = "2026-05-01",
    revision_no: int = 0,
    fmt: str = "xbrl",
    status: str = "parsed",
    period_end: str = "2026-03-31",
) -> None:
    """Insert a filings row."""
    with transaction() as conn:
        conn.execute(
            sa.insert(tables.filings).values(
                filing_id=filing_id,
                isin=ISIN,
                source="test",
                filing_type="financial_results",
                submission="revision" if revision_no else "original",
                revision_no=revision_no,
                taxonomy_family="indas",
                basis="consolidated",
                period_end=period_end,
                filed_on=filed_on,
                format=fmt,
                status=status,
                sha256=filing_id,
                raw_path="x",
                created_at="2026-05-01T00:00:00",
            )  # fmt: skip
        )


def add_parsed(filing_id: str, extractor: str = "xbrl", confidence: float = 1.0) -> int:
    """Insert a parsed_facts row (candidates point at one) -> its id."""
    with transaction() as conn:
        result = conn.execute(
            sa.insert(tables.parsed_facts).values(
                filing_id=filing_id, extractor=extractor, confidence=confidence
            )
        )
    return int(result.inserted_primary_key[0])


def cand(
    filing_id: str,
    period: str,
    value: str,
    concept: str = "revenue",
    extractor: str = "xbrl",
    confidence: float = 1.0,
    filed_on: str | None = "2026-05-01",
    revision_no: int = 0,
    unit: str = "INR",
) -> CanonicalCandidate:
    """A candidate for FY26 period `period` (Q1..Q4, H1, H2, 9M, FY)."""
    start, end, kind = FY26[period]
    return CanonicalCandidate(
        isin=ISIN, filing_id=filing_id, parsed_fact_id=add_parsed(filing_id, extractor, confidence),
        concept=concept, period_start=start, period_end=end, period_type=kind,
        basis="consolidated", value=D(value), unit=unit, mapping_method="standard",
        extractor=extractor, ocr_backend=None, confidence=confidence, filed_on=filed_on,
        revision_no=revision_no,
    )  # fmt: skip


@pytest.fixture
def kit(db: None) -> SimpleNamespace:
    """Helpers for tests (tests cannot import from conftest)."""
    return SimpleNamespace(
        add_filing=add_filing, cand=cand, write=write_candidates, ISIN=ISIN, D=D, FY26=FY26,
    )  # fmt: skip
