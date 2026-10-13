"""Checks against the real canonical_facts in data/finrag.db (skipped until it is populated).

The real database is opened read-only; `evaluate_isin` reads and returns, it writes nothing.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from finrag.core.db import tables
from finrag.core.types import ValidationStatus
from finrag.validation.cross_source import OCR_ERROR, RESTATED
from finrag.validation.engine import evaluate_isin
from finrag.validation.rules import load_rules

REAL = Path(__file__).resolve().parents[2]
DB = REAL / "data" / "finrag.db"
NETWEB, GABRIEL, HDFC = "INE0NT901020", "INE524A01029", "INE040A01034"
NETWEB_PDF = "FIL-84bf2a7b31af"
MN = Decimal(1_000_000)


def _engine() -> sa.engine.Engine | None:
    """Read-only engine on the real database, or None when it has no canonical facts."""
    if not DB.exists():
        return None
    engine = sa.create_engine(f"sqlite:///file:{DB.as_posix()}?mode=ro&uri=true")
    try:
        with engine.connect() as conn:
            found = conn.execute(sa.select(sa.func.count()).select_from(tables.canonical_facts))
            if not found.scalar():
                return None
    except sa.exc.OperationalError:
        return None
    return engine


ENGINE = _engine()
pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(ENGINE is None, reason="real canonical_facts not populated"),
]


@pytest.fixture(scope="module")
def evaluations():
    """ISIN -> Evaluation of the real data (computed once)."""
    return {
        isin: evaluate_isin(isin, load_rules(), engine=ENGINE) for isin in (NETWEB, GABRIEL, HDFC)
    }


def statuses(evaluation, rule_id: str) -> Counter:
    """Status -> number of results of a rule."""
    return Counter(r.status for r in evaluation.results if r.rule_id == rule_id)


def test_every_balance_sheet_balances_banks_included(evaluations) -> None:
    assert statuses(evaluations[GABRIEL], "bs_balances") == {ValidationStatus.PASS: 3}
    assert statuses(evaluations[NETWEB], "bs_balances") == {ValidationStatus.PASS: 3}
    assert statuses(evaluations[HDFC], "bank_bs_balances") == {
        ValidationStatus.PASS: 6
    }  # 3 x 2 bases


def test_opening_cash_equals_prior_closing_on_real_data(evaluations) -> None:
    rule = "opening_cash_equals_prior_closing"
    assert statuses(evaluations[GABRIEL], rule) == {
        ValidationStatus.PASS: 1,
        ValidationStatus.SKIP: 1,
    }
    assert statuses(evaluations[NETWEB], rule) == {
        ValidationStatus.PASS: 1,
        ValidationStatus.SKIP: 1,
    }
    assert statuses(evaluations[HDFC], rule) == {ValidationStatus.PASS: 2, ValidationStatus.SKIP: 2}
    for evaluation in evaluations.values():
        for row in evaluation.results:
            if row.rule_id == rule and row.status is ValidationStatus.SKIP:
                assert row.period_end == "2024-03-31"  # FY25's opening: there is no FY24 closing
                assert row.details["missing"] == ["closing_cash"]


def test_income_sums_hold_on_real_data(evaluations) -> None:
    for isin, rule in (
        (GABRIEL, "pl_total_income"),
        (NETWEB, "pl_total_income"),
        (HDFC, "bank_pl_total_income"),
    ):
        found = statuses(evaluations[isin], rule)
        assert set(found) == {ValidationStatus.PASS}, (isin, found)
        assert sum(found.values()) >= 9


def test_nothing_in_the_real_data_is_blocked(evaluations) -> None:
    for evaluation in evaluations.values():
        assert not [r for r in evaluation.results if r.status is ValidationStatus.BLOCK]


def test_gabriel_cash_flow_differs_from_balance_sheet_cash(evaluations) -> None:
    rows = {r.period_end: r for r in evaluations[GABRIEL].results if r.rule_id == "cash_ties_to_bs"}
    assert {k: v.status for k, v in rows.items()} == dict.fromkeys(rows, ValidationStatus.WARN)
    gaps = {k: Decimal(v.details["abs_diff"]) / MN for k, v in rows.items()}
    expected = {"2025-03-31": "38.65", "2025-09-30": "34.41", "2026-03-31": "15.08"}
    assert gaps == {k: Decimal(v) for k, v in expected.items()}
    assert statuses(evaluations[NETWEB], "cash_ties_to_bs") == {ValidationStatus.PASS: 3}


def netweb_rows(evaluations):
    return [r for r in evaluations[NETWEB].results if r.rule_id == "xbrl_vs_pdf"]


def test_netweb_cross_source_findings(evaluations) -> None:
    rows = netweb_rows(evaluations)
    warned = {(r.period_end, r.period_type): r for r in rows if r.status is ValidationStatus.WARN}
    assert set(warned) == {("2025-03-31", "INSTANT"), ("2026-03-31", "Q")}
    assert set(warned[("2025-03-31", "INSTANT")].details["concepts"]) == {
        "total_assets", "total_equity_and_liabilities", "total_equity", "other_equity",
        "total_current_assets",
    }  # fmt: skip
    assert warned[("2026-03-31", "Q")].details["concepts"] == ["profit_before_tax"]


def test_netweb_fy25_is_a_restated_comparative(evaluations) -> None:
    found = [  # the one cluster is shown on every row (FY, Q, INSTANT) it touches
        c
        for r in netweb_rows(evaluations)
        for c in r.details.get("clusters", [])
        if c["classification"] == RESTATED
    ]
    assert len(found) == 3 and all(c == found[0] for c in found)
    cluster = found[0]
    assert (cluster["pdf_filing"], cluster["period_end"], cluster["differing_keys"]) == (
        NETWEB_PDF, "2025-03-31", 21,
    )  # fmt: skip
    assert cluster["periods_affected"] == ["FY", "INSTANT", "Q"]
    holds = {(e["identity"], e["period_end"]): e["pdf_holds"] for e in cluster["identities"]}
    assert holds and all(holds.values())  # every identity that includes a restated line holds
    assert {"profit_before_tax", "profit_after_tax", "total_expenses", "total_equity"} <= {
        i for i, _ in holds
    }
    fy = next(
        r for r in netweb_rows(evaluations) if (r.period_end, r.period_type) == ("2025-03-31", "FY")
    )
    assert fy.status is ValidationStatus.PASS  # 9.68 mn is under the absolute tolerance
    pbt = next(d for d in fy.details["differences"] if d["concept"] == "profit_before_tax")
    assert (Decimal(pbt["xbrl"]) / MN, Decimal(pbt["pdf"]) / MN) == (
        Decimal("1539.68"),
        Decimal("1530.00"),
    )
    pat = next(d for d in fy.details["differences"] if d["concept"] == "profit_after_tax")
    assert (Decimal(pat["xbrl"]) / MN, Decimal(pat["pdf"]) / MN) == (
        Decimal("1144.75"),
        Decimal("1137.51"),
    )
    assert [(c.pdf_filing_id, str(c.period_end)) for _, c, _ in evaluations[NETWEB].restated] == [
        (NETWEB_PDF, "2025-03-31")
    ]


def test_netweb_q4_fy26_pbt_is_an_ocr_error(evaluations) -> None:
    row = next(
        r for r in netweb_rows(evaluations) if (r.period_end, r.period_type) == ("2026-03-31", "Q")
    )
    [cluster] = row.details["clusters"]
    assert cluster["classification"] == OCR_ERROR and cluster["differing_keys"] == 1
    [diff] = row.details["differences"]
    assert (Decimal(diff["xbrl"]), Decimal(diff["pdf"])) == (
        Decimal(948_220_000),
        Decimal(94_822_000_000),
    )
    assert set(diff["signals"]) == {"decimal_shift", "low_confidence"}
