"""Bank metrics, the standalone fallback for NPA and CET1, and company metrics on a bank."""

from __future__ import annotations

from decimal import Decimal

import pytest

from finrag.core.types import MetricStatus
from finrag.metrics.engine import run_metrics
from finrag.metrics.query import get_metric

D = Decimal
MN = D(1_000_000)
FY26, FY25 = ("2025-04-01", "2026-03-31", "FY"), ("2024-04-01", "2025-03-31", "FY")
END26, END25 = "2026-03-31", "2025-03-31"
Q4 = ("2026-01-01", END26, "Q")


def get(kit, metric: str, span, basis: str = "consolidated"):
    period = kit.period(span, span, "INSTANT") if isinstance(span, str) else kit.period(*span)
    return get_metric(kit.ISIN, metric, period, basis)


@pytest.fixture
def bank(kit):
    kit.sector("bank")
    kit.load_bank()
    run_metrics(kit.ISIN)
    return kit


@pytest.mark.parametrize(
    ("metric", "span", "expected"),
    [
        ("nii", FY26, (D(300) - D(120)) * MN),
        ("cost_to_income", FY26, D(90) / (D(180) + D(60))),
        ("credit_cost", FY26, D(30) / ((D(2000) + D(1600)) / 2)),
        ("advances_growth_yoy", END26, D(2000) / D(1600) - 1),
        ("deposits_growth_yoy", END26, D(2500) / D(2000) - 1),
        ("credit_deposit_ratio", END26, D(2000) / D(2500)),
        ("bank_equity", END26, (D(40) + D(260)) * MN),
        ("roe_bank", FY26, D(66) / ((D(300) + D(260)) / 2)),
        ("pat_growth_yoy", FY26, (D(70) - D(60)) / D(60)),
    ],
)
def test_bank_metrics_match_hand_values(bank, metric, span, expected) -> None:
    v = get(bank, metric, span)
    assert v.status is MetricStatus.COMPUTED, v.reason
    assert v.value == expected


def test_credit_cost_needs_last_years_advances(bank) -> None:
    v = get(bank, "credit_cost", FY25)
    assert v.code == "missing_input" and v.details["missing"][0]["alias"] == "adv_ly"


def test_company_metrics_are_not_applicable_to_a_bank(bank) -> None:
    for metric in ("operating_margin", "dso", "current_ratio", "roe", "roce", "fcf", "ebitda"):
        span = END26 if metric == "current_ratio" else FY26
        v = get(bank, metric, span)
        assert v.status is MetricStatus.NOT_COMPUTED and v.code == "not_applicable", metric
        assert "bank" in v.reason


def test_bank_metrics_are_not_applicable_to_a_general_company(kit) -> None:
    kit.load_acme()
    run_metrics(kit.ISIN)
    assert get(kit, "nii", FY26).code == "not_applicable"
    assert get(kit, "roe_bank", FY26).code == "not_applicable"
    assert get(kit, "gross_npa_ratio", FY26).code == "not_applicable"


# --- NPA and CET1: consolidated falls back to standalone ------------------------------------------


def _npa(kit, with_consolidated: bool = False) -> None:
    kit.sector("bank")
    kit.load_bank()
    for concept, value in (
        ("bank_gross_npa_ratio", "0.0115"),
        ("bank_net_npa_ratio", "0.0038"),
        ("bank_cet1_ratio", "0.1971"),
    ):
        kit.put(concept, *Q4[:2], value, "Q", basis="standalone", raw=True, unit="RATIO")
    kit.put("profit_after_tax", *Q4[:2], 18, "Q")  # the consolidated Q4 filing exists
    kit.put("bank_gross_npa", *Q4[:2], 340, "Q", basis="standalone")
    kit.put("bank_net_npa", *Q4[:2], 111, "Q", basis="standalone")
    if with_consolidated:
        kit.put("bank_gross_npa_ratio", *Q4[:2], "0.0099", "Q", raw=True, unit="RATIO")
    run_metrics(kit.ISIN)


def test_consolidated_npa_uses_the_reported_standalone_figure_and_says_so(kit) -> None:
    _npa(kit)
    for metric, expected in (
        ("gross_npa_ratio", D("0.0115")),
        ("net_npa_ratio", D("0.0038")),
        ("cet1_ratio", D("0.1971")),
        ("gross_npa", D(340) * MN),
        ("net_npa", D(111) * MN),
    ):
        v = get(kit, metric, Q4)
        assert v.status is MetricStatus.COMPUTED and v.value == expected, metric
        assert v.basis == "consolidated"  # the requested basis
        assert v.basis_used == "standalone"
        assert v.details["basis_fallback"] == "standalone"
        assert "standalone figures" in v.reason


def test_standalone_npa_is_read_directly_without_a_fallback_flag(kit) -> None:
    _npa(kit)
    v = get(kit, "gross_npa_ratio", Q4, basis="standalone")
    assert v.value == D("0.0115") and "basis_fallback" not in v.details
    assert v.basis_used == "standalone"


def test_a_consolidated_fact_wins_when_the_bank_does_report_one(kit) -> None:
    _npa(kit, with_consolidated=True)
    v = get(kit, "gross_npa_ratio", Q4)
    assert v.value == D("0.0099") and "basis_fallback" not in v.details


def test_npa_period_without_any_filing_has_no_row(kit) -> None:
    """FY26 has P&L facts but no NPA fact on either basis: no metric row, nothing guessed."""
    _npa(kit)
    assert get(kit, "gross_npa_ratio", FY26) is None


def test_the_ratio_is_reported_not_recomputed(kit) -> None:
    _npa(kit)
    v = get(kit, "gross_npa_ratio", Q4)
    assert v.value == D("0.0115")  # 340 / 2000 would be 0.17: advances are not an input
    assert len(v.input_fact_ids) == 1


def test_a_block_on_the_standalone_balance_sheet_does_not_touch_a_reported_ratio(kit) -> None:
    _npa(kit)
    kit.result("bank_bs_balances", "block", END26, "INSTANT", [], basis="standalone")
    run_metrics(kit.ISIN)
    assert get(kit, "gross_npa_ratio", Q4).status is MetricStatus.COMPUTED
