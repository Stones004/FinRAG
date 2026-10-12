"""Fixtures for M04: a small synthetic taxonomy, a temp database, and a collecting writer."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
import yaml

from finrag.core.db import init_db, tables, transaction
from finrag.normalize.mapper import Mapper
from finrag.normalize.models import CanonicalCandidate
from finrag.normalize.taxonomy import Taxonomy, parse_taxonomy

ISIN = "INE111A01010"
BANK_ISIN = "INE222A01012"
FILING = "FIL-aaaaaaaaaaaa"
PDF_FILING = "FIL-bbbbbbbbbbbb"
XBRL_UNITS = {
    "INR": ["iso4217:INR"],
    "INR_PER_SHARE": ["iso4217:INR/xbrli:shares"],
    "SHARES": ["xbrli:shares"],
    "RATIO": ["xbrli:pure"],
}


def _concept(statement: str, kind: str, unit: str = "INR", **extra: Any) -> dict[str, Any]:
    """A concept dict with the required fields."""
    return {"label": extra.pop("label", "x"), "statement": statement, "period_kind": kind,
            "unit": unit, **extra}  # fmt: skip


def taxonomy_data() -> dict[str, Any]:
    """A small taxonomy covering every mapping path the tests use."""
    return {
        "version": 1,
        "families": ["indas", "banking"],
        "xbrl_units": XBRL_UNITS,
        "unmapped_reasons": {
            "cash_flow_adjustment": "adjustments",
            "segment": "segments",
            "ratio": "ratios",
        },
        "concepts": {
            "revenue": _concept(
                "profit_and_loss",
                "duration",
                sectors=["general"],
                xbrl_concepts={"indas": ["in-capmkt:RevenueFromOperations"]},
                labels=["Revenue from operations", "Net sales"],
            ),  # fmt: skip
            "profit_after_tax": _concept(
                "profit_and_loss",
                "duration",
                xbrl_concepts={
                    "indas": ["in-capmkt:ProfitLossForPeriod"],
                    "banking": ["in-capmkt:ProfitLossForThePeriod"],
                },
                labels=["Profit for the period"],
            ),  # fmt: skip
            "other_comprehensive_income": _concept(
                "profit_and_loss",
                "duration",
                xbrl_concepts={"indas": ["in-capmkt:OtherComprehensiveIncome"]},
            ),  # fmt: skip
            "eps_basic": _concept(
                "profit_and_loss",
                "duration",
                "INR_PER_SHARE",
                xbrl_concepts={"indas": ["in-capmkt:BasicEps"]},
                labels=["Basic EPS"],
            ),  # fmt: skip
            "total_assets": _concept(
                "balance_sheet",
                "instant",
                xbrl_concepts={"indas": ["in-capmkt:Assets"], "banking": ["in-capmkt:Assets"]},
                labels=["Total assets"],
            ),  # fmt: skip
            "cfo": _concept(
                "cash_flow",
                "duration",
                xbrl_concepts={"indas": ["in-capmkt:CashFlowsFromOperating"]},
                labels=["Net cash from operating activities"],
            ),  # fmt: skip
            "dividends_paid": _concept(
                "cash_flow",
                "duration",
                sign_convention="outflow_positive",
                pdf_sign="negate",
                xbrl_concepts={"indas": ["in-capmkt:DividendsPaid"]},
                labels=["Dividends paid"],
            ),  # fmt: skip
            "bank_gross_npa": _concept(
                "other",
                "duration",
                sectors=["bank"],
                zero_is_placeholder=True,
                derivable=False,
                xbrl_concepts={"banking": ["in-capmkt:GrossNonPerformingAssets"]},
            ),  # fmt: skip
            "finance_cost": _concept(
                "profit_and_loss",
                "duration",
                labels=["Finance costs"],
                xbrl_concepts={"indas": ["in-capmkt:FinanceCosts"]},
            ),  # fmt: skip
            "closing_cash": _concept(
                "cash_flow",
                "instant",
                opening_concept="opening_cash",
                xbrl_concepts={"indas": ["in-capmkt:CashCfs"]},
                labels=["Cash at the end of the year"],
            ),  # fmt: skip
            "opening_cash": _concept(
                "cash_flow", "instant", labels=["Cash at the beginning of the year"]
            ),
            "bank_deposits": _concept(
                "balance_sheet",
                "instant",
                sectors=["bank"],
                xbrl_concepts={"banking": ["in-capmkt:Deposits"]},
            ),  # fmt: skip
        },
        "known_unmapped": {
            "indas": {
                "in-capmkt:AdjustmentsForWorkingCapital": "cash_flow_adjustment",
                "in-capmkt:OtherComprehensiveIncomeNetOfTaxes": {
                    "reason": "duplicate_of:other_comprehensive_income",
                    "note": "equal to OtherComprehensiveIncome",
                },
            },
            "banking": {
                "in-capmkt:SegmentRevenue": {"reason": "segment", "note": "never revenue"},
            },
        },
    }


@pytest.fixture
def taxonomy() -> Taxonomy:
    """The synthetic taxonomy."""
    return parse_taxonomy(taxonomy_data())


@pytest.fixture
def mapper(taxonomy: Taxonomy) -> Mapper:
    """A mapper with the synthetic taxonomy and a fuzzy threshold of 85."""
    return Mapper(taxonomy, fuzzy_min_score=85)


@pytest.fixture
def taxonomy_file(isolated_repo: Path) -> Path:
    """Write the synthetic taxonomy where settings.paths.taxonomy points (for CLI tests)."""
    path = isolated_repo / "config" / "taxonomy.yaml"
    path.write_text(yaml.safe_dump(taxonomy_data(), sort_keys=False), encoding="utf-8")
    return path


class Collector:
    """A CandidateWriter that records what it was given."""

    def __init__(self) -> None:
        self.calls: list[list[CanonicalCandidate]] = []

    def __call__(self, candidates: Sequence[CanonicalCandidate]) -> None:
        self.calls.append(list(candidates))

    @property
    def all(self) -> list[CanonicalCandidate]:
        """Every candidate received, in order."""
        return [c for call in self.calls for c in call]


@pytest.fixture
def writer() -> Collector:
    """A collecting writer."""
    return Collector()


@pytest.fixture
def db() -> None:
    """Create the temp database with two entities (a company and a bank)."""
    init_db()
    with transaction() as conn:
        for isin, sector in ((ISIN, "general"), (BANK_ISIN, "bank")):
            conn.execute(
                sa.insert(tables.entities).values(
                    isin=isin,
                    name=isin,
                    sector_type=sector,
                    default_basis="consolidated",
                    status="active",
                )  # fmt: skip
            )


def add_filing(
    filing_id: str = FILING,
    isin: str = ISIN,
    fmt: str = "xbrl",
    family: str | None = "indas",
    basis: str = "consolidated",
    status: str = "parsed",
) -> None:
    """Insert a filings row."""
    with transaction() as conn:
        conn.execute(
            sa.insert(tables.filings).values(
                filing_id=filing_id,
                isin=isin,
                source="test",
                filing_type="financial_results",
                submission="original",
                revision_no=0,
                taxonomy_family=family,
                basis=basis,
                period_start="2026-01-01",
                period_end="2026-03-31",
                period_type="Q",
                filed_on="2026-05-01",
                format=fmt,
                status=status,
                sha256=filing_id,
                raw_path="x",
                created_at="2026-05-01T00:00:00",
            )  # fmt: skip
        )


def add_fact(
    value: str | None,
    concept: str | None = None,
    label: str | None = None,
    start: date | None = date(2026, 1, 1),
    end: date | None = date(2026, 3, 31),
    unit: str | None = "iso4217:INR",
    scale: int | None = 0,
    extractor: str = "xbrl",
    confidence: float = 1.0,
    basis: str = "consolidated",
    dimensions: list[dict[str, str]] | None = None,
    filing_id: str = FILING,
    statement: str | None = None,
) -> int:
    """Insert a parsed_facts row -> its id."""
    with transaction() as conn:
        result = conn.execute(
            sa.insert(tables.parsed_facts).values(
                filing_id=filing_id,
                concept_raw=concept,
                label_raw=label,
                period_start=start.isoformat() if start else None,
                period_end=end.isoformat() if end else None,
                basis=basis,
                value=value,
                unit_raw=unit,
                scale=scale,
                decimals=-4,
                dimensions_json=json.dumps(dimensions) if dimensions else None,
                statement_type=statement,
                extractor=extractor,
                confidence=confidence,
            )  # fmt: skip
        )
    return int(result.inserted_primary_key[0])


@pytest.fixture
def kit(db: None) -> SimpleNamespace:
    """Helpers and constants for tests (tests cannot import from conftest)."""
    return SimpleNamespace(
        add_filing=add_filing, add_fact=add_fact, ISIN=ISIN, BANK_ISIN=BANK_ISIN, FILING=FILING,
        PDF_FILING=PDF_FILING, D=Decimal, taxonomy_data=taxonomy_data,
    )  # fmt: skip
