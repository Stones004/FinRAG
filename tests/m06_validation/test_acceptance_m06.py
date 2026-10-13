"""M06 acceptance criteria (one test per criterion in docs/modules/M06_validation.md)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
import yaml

from finrag.core.config import get_settings
from finrag.core.db import tables, transaction
from finrag.core.types import ValidationStatus
from finrag.normalize.taxonomy import load_taxonomy
from finrag.validation.engine import is_blocked, run_validation, warnings_for
from finrag.validation.rules import RulesError, load_rules, parse_rules

BASIS = "consolidated"
MAR26 = ("2026-03-31", "2026-03-31")
MAR25 = ("2025-03-31", "2025-03-31")
Q4 = ("2026-01-01", "2026-03-31")


def balance_sheet(kit, end: str = "2026-03-31", assets: str = "1000", liabilities: str = "1000"):
    """Write total assets and total equity and liabilities (millions) at a date."""
    kit.put("total_assets", end, end, kit.mn(assets))
    kit.put("total_equity_and_liabilities", end, end, kit.mn(liabilities))


def result(report, rule_id: str, end: str | None = None):
    """The single result of a rule (at a period end, when given)."""
    found = [r for r in report.results if r.rule_id == rule_id and end in (None, r.period_end)]
    assert len(found) == 1, found
    return found[0]


def instant(kit, end: str = "2026-03-31"):
    """An INSTANT period at a date."""
    return kit.period(end, end, "INSTANT")


def test_1_balanced_fixture_passes(kit) -> None:
    balance_sheet(kit)
    report = run_validation(kit.ISIN)
    assert result(report, "bs_balances").status is ValidationStatus.PASS
    assert is_blocked(kit.ISIN, "total_equity", instant(kit), BASIS) == (False, [])


def test_2_difference_under_tolerance_passes_over_blocks(kit) -> None:
    balance_sheet(kit, "2025-03-31", "1000", "1000.005")  # 5,000 rupees: under both tolerances
    balance_sheet(kit, "2026-03-31", "1000", "1050")  # 50 mn and 4.8 %: over both
    report = run_validation(kit.ISIN)
    assert result(report, "bs_balances", "2025-03-31").status is ValidationStatus.PASS
    over = result(report, "bs_balances", "2026-03-31")
    assert over.status is ValidationStatus.BLOCK
    assert over.details["abs_diff"] == "50000000"
    blocked, reasons = is_blocked(kit.ISIN, "total_equity", instant(kit), BASIS)
    assert (blocked, reasons) == (True, ["bs_balances"])
    assert is_blocked(kit.ISIN, "total_equity", instant(kit, "2025-03-31"), BASIS)[0] is False
    assert is_blocked(kit.ISIN, "revenue", instant(kit), BASIS)[0] is False  # not in the rule
    # a duration is blocked by the balances at its end ...
    fy26 = kit.period("2025-04-01", "2026-03-31", "FY")
    assert is_blocked(kit.ISIN, "total_assets", fy26, BASIS) == (True, ["bs_balances"])
    # ... and not by the balance it starts from, unless the metric averages opening and closing
    fy27 = kit.period("2026-04-01", "2027-03-31", "FY")
    assert is_blocked(kit.ISIN, "total_assets", fy27, BASIS)[0] is False
    assert is_blocked(kit.ISIN, "total_assets", fy27, BASIS, include_opening=True)[0] is True


def test_2a_either_tolerance_alone_does_not_fail(kit) -> None:
    kit.put("total_assets", *MAR26, kit.mn("100000"))
    kit.put("total_equity_and_liabilities", *MAR26, kit.mn("100050"))  # 50 mn but 0.05 %
    kit.put("total_assets", *MAR25, kit.mn("1000"))
    kit.put("total_equity_and_liabilities", *MAR25, kit.mn("1001.5"))  # 0.15 % but only 1.5 mn
    report = run_validation(kit.ISIN)
    assert {r.status for r in report.results if r.rule_id == "bs_balances"} == {
        ValidationStatus.PASS
    }


def test_3_missing_input_skips_and_names_it(kit) -> None:
    kit.put("total_assets", *MAR26, kit.mn("1000"))
    report = run_validation(kit.ISIN)
    skipped = result(report, "bs_balances")
    assert skipped.status is ValidationStatus.SKIP
    assert skipped.details["missing"] == ["total_equity_and_liabilities"]
    assert (
        is_blocked(kit.ISIN, "total_assets", instant(kit), BASIS)[0] is False
    )  # skip never blocks


def test_4_sector_filter(kit) -> None:
    balance_sheet(kit)
    general = run_validation(kit.ISIN)
    assert {r.rule_id for r in general.results if "bs_balances" in r.rule_id} == {"bs_balances"}
    kit.sector("bank")
    bank = run_validation(kit.ISIN)
    assert {r.rule_id for r in bank.results if "bs_balances" in r.rule_id} == {"bank_bs_balances"}


def test_5_rerun_appends_a_new_run_with_identical_statuses(kit) -> None:
    balance_sheet(kit, assets="1000", liabilities="1100")
    first = run_validation(kit.ISIN)
    second = run_validation(kit.ISIN)
    assert first.run_id != second.run_id
    assert [(r.rule_id, r.status) for r in first.results] == [
        (r.rule_id, r.status) for r in second.results
    ]
    with transaction() as conn:
        rows = conn.execute(
            sa.select(sa.func.count()).select_from(tables.validation_results)
        ).scalar()
    assert rows == len(first.results) * 2  # append-only
    assert is_blocked(kit.ISIN, "total_assets", instant(kit), BASIS)[0] is True


def test_5a_a_later_pass_replaces_an_earlier_block(kit) -> None:
    balance_sheet(kit, assets="1000", liabilities="1100")
    run_validation(kit.ISIN)
    assert is_blocked(kit.ISIN, "total_assets", instant(kit), BASIS)[0] is True
    kit.put(
        "total_equity_and_liabilities", *MAR26, kit.mn("1000"), filing="FIL-fix",
        filed_on="2026-06-01",
    )  # fmt: skip
    run_validation(kit.ISIN)
    assert is_blocked(kit.ISIN, "total_assets", instant(kit), BASIS) == (False, [])


def test_6_new_sum_rule_needs_no_code(kit, isolated_repo) -> None:
    kit.put("employee_expense", *Q4, kit.mn("100"), "Q")
    kit.put("finance_cost", *Q4, kit.mn("10"), "Q")
    kit.put("total_expenses", *Q4, kit.mn("500"), "Q")
    path = isolated_repo / "config" / "validation_rules.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    new_rule = {
        "id": "two_costs_equal_total",
        "type": "sum",
        "parts": ["employee_expense", "finance_cost"],
        "total": "total_expenses",
        "severity_on_fail": "warn",
    }
    data["rules"].append(new_rule)
    path.write_text(yaml.safe_dump(data), "utf-8")
    report = run_validation(kit.ISIN)
    added = result(report, "two_costs_equal_total")
    assert added.status is ValidationStatus.WARN
    assert added.details["sum"] == "110000000"


# --- a deliberately broken case for each rule -------------------------------------------------

# Test-only stand-in for the M07 metric registry: metric -> the concepts it reads.
METRIC_INPUTS = {
    "debt_to_equity": ["long_term_borrowings", "short_term_borrowings", "total_equity"],
    "equity_ratio": ["total_equity", "total_assets"],
    "net_cash_change": ["opening_cash", "closing_cash"],
    "operating_margin": ["revenue", "total_income"],
    "loan_to_deposit": ["bank_advances", "bank_deposits"],
}


def blocked_metrics(kit, period, basis: str = BASIS) -> set[str]:
    """Metrics that would be blocked: any input concept blocked at the period."""
    return {
        metric
        for metric, inputs in METRIC_INPUTS.items()
        if any(is_blocked(kit.ISIN, c, period, basis)[0] for c in inputs)
    }


def test_broken_bs_balances_blocks_its_dependent_metrics(kit) -> None:
    balance_sheet(kit, assets="1000", liabilities="900")
    run_validation(kit.ISIN)
    assert blocked_metrics(kit, instant(kit)) == {"debt_to_equity", "equity_ratio"}


def test_broken_bank_balance_sheet_blocks_bank_concepts(kit) -> None:
    kit.sector("bank")
    balance_sheet(kit, assets="1000", liabilities="900")
    run_validation(kit.ISIN)
    assert is_blocked(kit.ISIN, "bank_deposits", instant(kit), BASIS) == (
        True,
        ["bank_bs_balances"],
    )
    assert blocked_metrics(kit, instant(kit)) == {"equity_ratio", "loan_to_deposit"}


def test_broken_income_sum_warns_and_blocks_nothing(kit) -> None:
    kit.put("revenue", *Q4, kit.mn("1000"), "Q")
    kit.put("other_income", *Q4, kit.mn("50"), "Q")
    kit.put("total_income", *Q4, kit.mn("1100"), "Q")  # should be 1050
    report = run_validation(kit.ISIN)
    row = result(report, "pl_total_income")
    assert (row.status, row.details["sum"], row.details["total"]) == (
        ValidationStatus.WARN, "1050000000", "1100000000",
    )  # fmt: skip
    quarter = kit.period(*Q4, "Q")
    assert blocked_metrics(kit, quarter) == set()  # a warning blocks nothing
    found = warnings_for(kit.ISIN, "total_income", quarter, BASIS)
    assert [w["rule_id"] for w in found] == ["pl_total_income"]
    assert warnings_for(kit.ISIN, "employee_expense", quarter, BASIS) == []


def test_broken_bank_income_sum_uses_interest_earned(kit) -> None:
    kit.sector("bank")
    kit.put("bank_interest_earned", *Q4, kit.mn("800"), "Q")
    kit.put("other_income", *Q4, kit.mn("200"), "Q")
    kit.put("total_income", *Q4, kit.mn("1000"), "Q")
    assert result(run_validation(kit.ISIN), "bank_pl_total_income").status is ValidationStatus.PASS
    kit.put("total_income", *Q4, kit.mn("1300"), "Q", filing="FIL-fix", filed_on="2026-06-01")
    report = run_validation(kit.ISIN)
    assert result(report, "bank_pl_total_income").status is ValidationStatus.WARN
    assert not [r for r in report.results if r.rule_id == "pl_total_income"]


def test_broken_cash_tie_warns_with_the_gap(kit) -> None:
    kit.put("closing_cash", *MAR26, kit.mn("339.76"))
    kit.put("cash_and_equivalents", *MAR26, kit.mn("378.41"))
    row = result(run_validation(kit.ISIN), "cash_ties_to_bs")
    assert row.status is ValidationStatus.WARN  # warn for everyone, never block
    assert (row.details["left"], row.details["right"], row.details["abs_diff"]) == (
        "339760000", "378410000", "38650000",
    )  # fmt: skip
    assert blocked_metrics(kit, instant(kit)) == set()


def test_broken_sign_and_range_warn(kit) -> None:
    kit.put("revenue", *Q4, -kit.mn("5"), "Q")
    kit.put("tax_expense", *Q4, kit.mn("90"), "Q")
    kit.put("profit_before_tax", *Q4, kit.mn("100"), "Q")
    report = run_validation(kit.ISIN)
    assert result(report, "revenue_non_negative").status is ValidationStatus.WARN
    row = result(report, "tax_rate_plausible")
    assert (row.status, row.details["ratio"]) == (ValidationStatus.WARN, "0.9")


def test_range_with_zero_denominator_skips(kit) -> None:
    kit.put("tax_expense", *Q4, kit.mn("1"), "Q")
    kit.put("profit_before_tax", *Q4, 0, "Q")
    assert result(run_validation(kit.ISIN), "tax_rate_plausible").status is ValidationStatus.SKIP


def test_broken_opening_vs_prior_closing_blocks_that_date_only(kit) -> None:
    kit.put("closing_cash", *MAR25, kit.mn("100"))
    kit.put("opening_cash", *MAR25, kit.mn("120"), filing="FIL-fy26")  # FY26 opens at 120
    kit.put("closing_cash", *MAR26, kit.mn("200"))
    kit.put("opening_cash", "2024-03-31", "2024-03-31", kit.mn("50"))  # no FY24 closing
    report = run_validation(kit.ISIN)
    by_date = {r.period_end: r for r in report.results if r.rule_id.startswith("opening_cash")}
    assert by_date["2025-03-31"].status is ValidationStatus.BLOCK
    assert by_date["2025-03-31"].details["abs_diff"] == "20000000"
    assert by_date["2024-03-31"].status is ValidationStatus.SKIP
    assert by_date["2024-03-31"].details["missing"] == ["closing_cash"]
    reason = ["opening_cash_equals_prior_closing"]
    for concept in ("opening_cash", "closing_cash"):
        assert is_blocked(kit.ISIN, concept, instant(kit, "2025-03-31"), BASIS) == (True, reason)
        assert is_blocked(kit.ISIN, concept, instant(kit, "2026-03-31"), BASIS)[0] is False
        assert is_blocked(kit.ISIN, concept, instant(kit, "2024-03-31"), BASIS)[0] is False
    assert is_blocked(kit.ISIN, "cfo", instant(kit, "2025-03-31"), BASIS)[0] is False
    assert blocked_metrics(kit, instant(kit, "2025-03-31")) == {"net_cash_change"}
    assert blocked_metrics(kit, instant(kit, "2026-03-31")) == set()


def test_a_plain_closing_request_does_not_see_the_opening_date(kit) -> None:
    kit.put("closing_cash", *MAR25, kit.mn("100"))
    kit.put("opening_cash", *MAR25, kit.mn("120"), filing="FIL-fy26")  # blocks 2025-03-31
    kit.put("closing_cash", *MAR26, kit.mn("200"))
    run_validation(kit.ISIN)
    fy26 = kit.period("2025-04-01", "2026-03-31", "FY")  # opens at 2025-03-31, closes at 2026-03-31
    fy25 = kit.period("2024-04-01", "2025-03-31", "FY")
    reason = ["opening_cash_equals_prior_closing"]
    assert is_blocked(kit.ISIN, "closing_cash", fy26, BASIS) == (False, [])
    assert is_blocked(kit.ISIN, "opening_cash", fy26, BASIS) == (True, reason)  # its opening date
    assert is_blocked(kit.ISIN, "closing_cash", fy25, BASIS) == (True, reason)  # its end date
    assert is_blocked(kit.ISIN, "opening_cash", fy25, BASIS)[0] is False  # opens at 2024-03-31
    # a metric averaging opening and closing balances asks for both dates
    assert is_blocked(kit.ISIN, "closing_cash", fy26, BASIS, include_opening=True) == (True, reason)


def test_matching_opening_and_closing_pass(kit) -> None:
    kit.put("closing_cash", *MAR25, kit.mn("100"))
    kit.put("opening_cash", *MAR25, kit.mn("100.005"), filing="FIL-fy26")  # rounding
    row = result(run_validation(kit.ISIN), "opening_cash_equals_prior_closing")
    assert row.status is ValidationStatus.PASS


# --- rule registry -----------------------------------------------------------------------------


def registry(isolated_repo):
    """The rule registry in the temp repo, as a dict."""
    return yaml.safe_load((isolated_repo / "config" / "validation_rules.yaml").read_text("utf-8"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"type": "magic"}, "type"),
        ({"left": "not_a_concept"}, "unknown concept 'not_a_concept'"),
        ({"blocks_concepts": []}, "severity block needs blocks_concepts"),
        ({"severity_on_fail": "warn"}, "blocks_concepts is only for severity block"),
        ({"period_kind": "duration"}, "does not match its concepts"),
        ({"right": "revenue"}, "do not share one period kind"),
    ],
)
def test_bad_rules_are_rejected_at_load(isolated_repo, change, message) -> None:
    data = registry(isolated_repo)
    data["rules"] = [{**data["rules"][0], **change}]
    with pytest.raises(RulesError, match=message):
        parse_rules(data, load_taxonomy())


def test_duplicate_rule_ids_are_rejected(isolated_repo) -> None:
    data = registry(isolated_repo)
    data["rules"] = [data["rules"][0], data["rules"][0]]
    with pytest.raises(RulesError, match="duplicate rule id"):
        parse_rules(data, load_taxonomy())


def test_real_registry_loads_with_every_rule_type() -> None:
    rules = load_rules()
    assert {r.type for r in rules.rules} == {
        "equality", "sum", "sign", "range", "cross_statement", "cross_period", "cross_source",
    }  # fmt: skip


def test_settings_have_the_cross_source_keys() -> None:
    cross = get_settings().validation.cross_source
    assert (cross.rounding_inr, cross.min_shifted_keys, cross.decimal_shift_max_power) == (
        10_000, 3, 3,
    )  # fmt: skip


def test_profit_after_tax_adds_associates_and_discontinued_operations(kit) -> None:
    kit.put("profit_before_tax", *Q4, kit.mn("1000"), "Q")
    kit.put("tax_expense", *Q4, kit.mn("300"), "Q")
    kit.put("profit_after_tax", *Q4, kit.mn("700"), "Q")
    assert result(run_validation(kit.ISIN), "pl_profit_after_tax").status is ValidationStatus.PASS
    kit.put("share_of_associates_jv", *Q4, kit.mn("400"), "Q")  # PAT must now include it
    row = result(run_validation(kit.ISIN), "pl_profit_after_tax")
    assert (row.status, row.details["sum"]) == (ValidationStatus.WARN, "1100000000")
    kit.put("profit_after_tax", *Q4, kit.mn("1100"), "Q", filing="FIL-fix", filed_on="2026-06-01")
    assert result(run_validation(kit.ISIN), "pl_profit_after_tax").status is ValidationStatus.PASS
    kit.put("profit_discontinued_ops", *Q4, -kit.mn("50"), "Q")  # a loss, signed as reported
    row = result(run_validation(kit.ISIN), "pl_profit_after_tax")
    assert (row.status, row.details["sum"]) == (ValidationStatus.WARN, "1050000000")


def test_associates_and_discontinued_concepts_are_mapped() -> None:
    taxonomy = load_taxonomy()
    by_qname = taxonomy.by_qname
    assert (
        by_qname[
            (
                "indas",
                "in-capmkt:ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod",
            )
        ]
        == "share_of_associates_jv"
    )
    assert (
        by_qname[("banking", "in-capmkt:ShareOfProfitLossOfAssociates")] == "share_of_associates_jv"
    )
    assert (
        by_qname[("indas", "in-capmkt:ProfitLossForPeriodFromContinuingOperations")]
        == "profit_continuing_ops"
    )
    assert (
        by_qname[("indas", "in-capmkt:ProfitLossFromDiscontinuedOperationsAfterTax")]
        == "profit_discontinued_ops"
    )
