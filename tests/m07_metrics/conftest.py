"""Fixtures for M07: a temp database with facts, events and validation results written directly."""

from __future__ import annotations

import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
import yaml

from finrag.core.db import init_db, tables, transaction
from finrag.core.ids import PREFIX_FACT, new_id
from finrag.core.types import Period, PeriodType, utc_now_iso

ISIN = "INE111A01010"
REAL = Path(__file__).resolve().parents[2]
MN = Decimal(1_000_000)
D = Decimal
KINDS = {"Q": PeriodType.Q, "H": PeriodType.H, "FY": PeriodType.FY, "9M": PeriodType.NINE_M}

FY26 = ("2025-04-01", "2026-03-31", "FY")
FY25 = ("2024-04-01", "2025-03-31", "FY")
END26, END25, END24 = "2026-03-31", "2025-03-31", "2024-03-31"


@pytest.fixture(autouse=True)
def registries(isolated_repo: Path) -> None:
    """Put the real registries where settings.paths points."""
    for name in ("taxonomy.yaml", "validation_rules.yaml", "metrics.yaml"):
        shutil.copy(REAL / "config" / name, isolated_repo / "config" / name)


def make_kit(root: Path) -> SimpleNamespace:
    """Helpers that write facts, events, restatements and validation results."""
    filings: set[str] = set()

    def sector(value: str) -> None:
        with transaction() as conn:
            conn.execute(
                sa.update(tables.entities)
                .where(tables.entities.c.isin == ISIN)
                .values(sector_type=value)
            )

    def ensure_filing(filing_id: str, fmt: str, filed_on: str, revision_no: int) -> None:
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
        mn: str | int | Decimal,
        ptype: str | None = None,
        basis: str = "consolidated",
        src: str = "xbrl",
        filing: str = "FIL-x",
        filed_on: str = "2026-05-01",
        revision_no: int = 0,
        unit: str = "INR",
        raw: bool = False,
        derived: bool = False,
    ) -> str:
        """Write one canonical fact (value in millions, or `raw` units) -> its fact_id."""
        ensure_filing(filing, "xbrl" if src == "xbrl" else "pdf", filed_on, revision_no)
        kind = "INSTANT" if start == end else ptype or "Q"
        value = D(mn) if raw else D(mn) * MN
        fact_id = new_id(PREFIX_FACT)
        with transaction() as conn:
            parsed = conn.execute(
                sa.insert(tables.parsed_facts).values(
                    filing_id=filing, extractor="xbrl", confidence=1.0
                )
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
                    value=format(value, "f"),
                    unit=unit,
                    source_filing_id=filing,
                    parsed_fact_id=parsed,
                    mapping_method="standard",
                    is_derived=1 if derived else 0,
                    derivation_json=json.dumps({"all_xbrl": True}) if derived else None,
                    filed_on=filed_on,
                    created_at=utc_now_iso(),
                    source_class="derived" if derived else src,
                )  # fmt: skip
            )
        return fact_id

    def event(kind: str, when: str, basis: str | None = None, citation: str = "Note 5") -> int:
        details = {"citation": citation}
        if basis:
            details["basis"] = basis
        with transaction() as conn:
            return int(
                conn.execute(
                    sa.insert(tables.corporate_events).values(
                        isin=ISIN,
                        event_type=kind,
                        event_date=when,
                        details=json.dumps(details),
                        source="test",
                    )
                ).inserted_primary_key[0]
            )

    def result(
        rule_id: str,
        status: str,
        end: str,
        kind: str,
        concepts: list[str],
        basis: str = "consolidated",
    ) -> None:
        """Write one validation result (the latest per rule/date/kind is current)."""
        with transaction() as conn:
            conn.execute(
                sa.insert(tables.validation_results).values(
                    isin=ISIN,
                    period_end=end,
                    period_type=kind,
                    basis=basis,
                    rule_id=rule_id,
                    status=status,
                    details_json=json.dumps({"concepts": concepts, "abs_diff": "1"}),
                    run_id="RUN-t",
                    created_at=utc_now_iso(),
                )  # fmt: skip
            )

    def restate(old_fact: str, new_fact: str, concept: str, end: str, flagged: int = 1) -> int:
        with transaction() as conn:
            return int(
                conn.execute(
                    sa.insert(tables.restatements).values(
                        isin=ISIN,
                        concept=concept,
                        period_end=end,
                        basis="consolidated",
                        old_fact_id=old_fact,
                        new_fact_id=new_fact,
                        abs_diff="1",
                        rel_diff="0.1",
                        flagged=flagged,
                        created_at=utc_now_iso(),
                    )  # fmt: skip
                ).inserted_primary_key[0]
            )

    def edit_registry(change) -> None:
        """Apply `change(dict)` to the temp copy of metrics.yaml."""
        path = root / "config" / "metrics.yaml"
        data = yaml.safe_load(path.read_text("utf-8"))
        change(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")

    def period(start: str, end: str, kind: str) -> Period:
        return Period(date.fromisoformat(start), date.fromisoformat(end), PeriodType(kind))

    return SimpleNamespace(
        put=put,
        sector=sector,
        event=event,
        result=result,
        restate=restate,
        edit_registry=edit_registry,
        period=period,
        ISIN=ISIN,
        D=D,
    )


@pytest.fixture
def kit(isolated_repo: Path) -> SimpleNamespace:
    """A temp database with one general-sector entity and fact-writing helpers."""
    init_db()
    with transaction() as conn:
        conn.execute(
            sa.insert(tables.entities).values(
                isin=ISIN,
                name="Acme",
                sector_type="general",
                default_basis="consolidated",
                status="active",
            )  # fmt: skip
        )
    kit = make_kit(isolated_repo)
    kit.load_acme = lambda: load_acme(kit)
    kit.load_bank = lambda: load_bank(kit)
    return kit


def load_acme(kit: SimpleNamespace) -> None:
    """A general company with FY25 and FY26 statements (values in millions, easy by hand)."""
    p = kit.put
    s26, e26, _ = FY26
    s25, e25, _ = FY25
    for concept, fy26, fy25 in (
        ("revenue", 1000, 800),
        ("other_income", 20, 10),
        ("finance_cost", 30, 25),
        ("depreciation_amortisation", 50, 40),
        ("profit_before_tax", 150, 100),
        ("tax_expense", 40, 20),
        ("profit_after_tax", 110, 80),
        ("share_of_associates_jv", 5, 0),
        ("cfo", 200, 150),
        ("capex_ppe", 60, 50),
    ):
        p(concept, s26, e26, fy26, "FY")
        p(concept, s25, e25, fy25, "FY")
    p("exceptional_items", s26, e26, -10, "FY")  # FY25 has none: assumed zero
    p("profit_attributable_owners", s26, e26, 100, "FY")
    p("profit_attributable_owners", s25, e25, 75, "FY")
    p("capex_intangibles", s26, e26, 20, "FY")
    for end, values in (
        (
            END26,
            {
                "total_equity": 600,
                "non_controlling_interest": 50,
                "long_term_borrowings": 100,
                "short_term_borrowings": 40,
                "total_current_assets": 500,
                "total_current_liabilities": 250,
                "trade_receivables_current": 150,
            },
        ),
        (
            END25,
            {
                "total_equity": 500,
                "non_controlling_interest": 40,
                "long_term_borrowings": 80,
                "short_term_borrowings": 20,
                "total_current_assets": 400,
                "total_current_liabilities": 300,
                "trade_receivables_current": 120,
            },
        ),
    ):
        for concept, value in values.items():
            p(concept, end, end, value)
    p("opening_cash", END24, END24, 30)  # an instant with no balance sheet: no metric rows


def load_bank(kit: SimpleNamespace) -> None:
    """A bank with FY25 and FY26 (values in millions)."""
    p = kit.put
    s26, e26, _ = FY26
    s25, e25, _ = FY25
    for concept, fy26, fy25 in (
        ("bank_interest_earned", 300, 280),
        ("bank_interest_expended", 120, 130),
        ("other_income", 60, 50),
        ("bank_operating_expenses", 90, 85),
        ("bank_provisions", 30, 20),
        ("profit_after_tax", 70, 60),
        ("profit_attributable_owners", 66, 58),
    ):
        p(concept, s26, e26, fy26, "FY")
        p(concept, s25, e25, fy25, "FY")
    for end, values in (
        (END26, {"bank_advances": 2000, "bank_deposits": 2500, "bank_share_capital": 40,
                 "bank_reserves_and_surplus": 260}),
        (END25, {"bank_advances": 1600, "bank_deposits": 2000, "bank_share_capital": 40,
                 "bank_reserves_and_surplus": 220}),
    ):  # fmt: skip
        for concept, value in values.items():
            p(concept, end, end, value)
