"""Fixtures for M06: a temp database with facts written straight to canonical_facts."""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from finrag.core.db import init_db, tables, transaction
from finrag.core.ids import PREFIX_FACT, new_id
from finrag.core.types import Period, PeriodType, utc_now_iso

ISIN = "INE111A01010"
REAL = Path(__file__).resolve().parents[2]
MN = Decimal(1_000_000)
D = Decimal
KINDS = {"Q": PeriodType.Q, "H": PeriodType.H, "FY": PeriodType.FY, "9M": PeriodType.NINE_M}


@pytest.fixture(autouse=True)
def registries(isolated_repo: Path) -> None:
    """Put the real taxonomy and validation rules where settings.paths points."""
    for name in ("taxonomy.yaml", "validation_rules.yaml"):
        shutil.copy(REAL / "config" / name, isolated_repo / "config" / name)


def make_kit() -> SimpleNamespace:
    """Fact-writing helpers as plain functions on a namespace (tests cannot import conftest)."""
    filings: set[str] = set()

    def sector(value: str) -> None:
        with transaction() as conn:
            conn.execute(
                sa.update(tables.entities)
                .where(tables.entities.c.isin == ISIN)
                .values(sector_type=value)
            )

    def ensure_filing(filing_id: str, fmt: str, filed_on: str | None, revision_no: int) -> None:
        if filing_id in filings:
            return
        filings.add(filing_id)
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
                    period_end="2026-03-31",
                    filed_on=filed_on,
                    format=fmt,
                    status="normalized",
                    sha256=filing_id,
                    raw_path="x",
                    created_at="2026-05-01T00:00:00",
                )  # fmt: skip
            )

    def put(
        concept: str,
        start: str,
        end: str,
        value: str | Decimal,
        ptype: str | None = None,
        src: str = "xbrl",
        filing: str | None = None,
        conf: float | None = None,
        basis: str = "consolidated",
        filed_on: str = "2026-05-01",
        revision_no: int = 0,
        unit: str = "INR",
    ) -> str:
        """Write one canonical fact (value in rupees) -> its fact_id."""
        filing = filing or ("FIL-x" if src == "xbrl" else "FIL-p")
        ensure_filing(filing, "xbrl" if src == "xbrl" else "pdf", filed_on, revision_no)
        kind = "INSTANT" if start == end else ptype or "Q"
        fact_id = new_id(PREFIX_FACT)
        with transaction() as conn:
            parsed = conn.execute(
                sa.insert(tables.parsed_facts).values(
                    filing_id=filing,
                    extractor="xbrl" if src == "xbrl" else "ocr",
                    confidence=1.0 if conf is None else conf,
                )  # fmt: skip
            ).inserted_primary_key[0]
            conn.execute(
                sa.insert(tables.canonical_facts).values(
                    fact_id=fact_id,
                    isin=ISIN,
                    concept=concept,
                    period_start=start,
                    period_end=end,
                    period_type=kind,
                    basis=basis,
                    value=format(D(value), "f"),
                    unit=unit,
                    source_filing_id=filing,
                    parsed_fact_id=parsed,
                    mapping_method="standard",
                    is_derived=0,
                    filed_on=filed_on,
                    created_at=utc_now_iso(),
                    source_class=src,
                )  # fmt: skip
            )
        return fact_id

    def mn(value: str) -> Decimal:
        """Millions -> rupees."""
        return (D(value) * MN).to_integral_value()

    def period(start: str, end: str, kind: str) -> Period:
        return Period(date.fromisoformat(start), date.fromisoformat(end), PeriodType(kind))

    return SimpleNamespace(put=put, mn=mn, sector=sector, period=period, ISIN=ISIN, D=D)


@pytest.fixture
def kit() -> SimpleNamespace:
    """A temp database with one general-sector entity, and fact-writing helpers."""
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
    return make_kit()
