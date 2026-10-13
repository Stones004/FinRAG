"""The metrics engine on a small hand-checkable company (M07 acceptance 1, 5-11 and the rules)."""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa

from finrag.core.db import tables, transaction
from finrag.core.types import MetricStatus
from finrag.metrics.engine import run_metrics
from finrag.metrics.query import get_metric

D = Decimal
MN = D(1_000_000)
FY26, FY25 = ("2025-04-01", "2026-03-31", "FY"), ("2024-04-01", "2025-03-31", "FY")
END26, END25 = "2026-03-31", "2025-03-31"
BASIS = "consolidated"


def value(kit, metric: str, start_end: tuple[str, str, str] | str, basis: str = BASIS):
    """Stored MetricValue for a metric at FY26 / FY25 or an instant date."""
    if isinstance(start_end, str):
        period = kit.period(start_end, start_end, "INSTANT")
    else:
        period = kit.period(*start_end)
    return get_metric(kit.ISIN, metric, period, basis)


@pytest.fixture
def acme(kit):
    kit.load_acme()
    run_metrics(kit.ISIN)
    return kit


def mn(x) -> Decimal:
    return D(x) * MN


# --- acceptance 1: every starter metric against a hand-computed value ----------------------------


def test_operating_profit_adds_back_finance_cost_and_exceptional_loss_and_drops_other_income(acme):
    # 150 (PBT) + 30 (finance) - 20 (other income) - (-10) (exceptional loss) = 170
    v = value(acme, "operating_profit", FY26)
    assert v.value == mn(170) and v.status is MetricStatus.COMPUTED
    # FY25 has no exceptional items: 100 + 25 - 10 - 0 = 115, with the zero recorded as assumed
    ly = value(acme, "operating_profit", FY25)
    assert ly.value == mn(115)
    assert [z["concept"] for z in ly.details["assumed_zero"]] == ["exceptional_items"]


def test_operating_profit_ignores_associates(acme):
    """Associates (5) are reported after PBT: they are neither added nor subtracted."""
    assert value(acme, "operating_profit", FY26).value == mn(170)


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        ("ebitda", mn(220)),  # 170 + 50
        ("operating_margin", D(170) / D(1000)),
        ("ebitda_margin", D(220) / D(1000)),
        ("net_margin", D(110) / D(1000)),
        ("revenue_growth_yoy", D(1000) / D(800) - 1),
        ("pat_growth_yoy", (D(110) - D(80)) / D(80)),
        ("operating_margin_change_yoy", D(170) / D(1000) - D(115) / D(800)),
        ("associates_share_of_pat", D(5) / D(110)),
        ("profit_owners", mn(100)),
        ("cfo_to_pat", D(200) / D(110)),
        ("fcf", mn(200 - 60 - 20)),
        ("dso", D(150) / D(1000) * D(365)),
        ("roe", D(100) / ((D(550) + D(460)) / 2)),  # owners' equity 600-50 and 500-40
        ("roce", D(170) / ((D(740) + D(600)) / 2)),  # equity + borrowings
    ],
)
def test_duration_metrics_match_hand_values(acme, metric: str, expected: Decimal) -> None:
    v = value(acme, metric, FY26)
    assert v.status is MetricStatus.COMPUTED, v.reason
    assert v.value == expected


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        ("equity_owners", mn(550)),
        ("capital_employed", mn(740)),
        ("debt_to_equity", D(140) / D(550)),
        ("current_ratio", D(500) / D(250)),
    ],
)
def test_instant_metrics_match_hand_values(acme, metric: str, expected: Decimal) -> None:
    v = value(acme, metric, END26)
    assert v.value == expected, v.reason


def test_formula_version_and_input_fact_ids_are_stored(acme) -> None:
    v = value(acme, "operating_profit", FY26)
    assert v.formula_version == "1.0"
    assert len(v.input_fact_ids) == 4 and all(f.startswith("FCT-") for f in v.input_fact_ids)
    growth = value(acme, "revenue_growth_yoy", FY26)
    assert len(growth.input_fact_ids) == 2  # revenue this year and last year
    assert v.metric_value_id.startswith("MET-")
    assert v.run_id.startswith("RUN-")


def test_reason_is_a_sentence_and_tokens_live_in_details(acme) -> None:
    v = value(acme, "capital_employed", END26)
    assert v.reason.startswith("Computed from reported facts.")
    assert "Assumed zero (not reported): lease_liabilities." in v.reason
    assert "assumed_zero" not in v.reason and ":" not in v.reason.split("Assumed zero")[0]
    assert v.details["code"] == "computed"
    assert v.details["assumed_zero"] == [
        {"alias": "ll", "concept": "lease_liabilities", "period_end": END26}
    ]


# --- missing inputs, applicability ----------------------------------------------------------------


def test_missing_last_year_revenue_gives_missing_input(kit) -> None:
    kit.load_acme()
    with transaction() as conn:  # drop FY25 revenue only
        conn.execute(
            sa.delete(tables.canonical_facts).where(
                tables.canonical_facts.c.concept == "revenue",
                tables.canonical_facts.c.period_end == END25,
            )
        )
    run_metrics(kit.ISIN)
    v = value(kit, "revenue_growth_yoy", FY26)
    assert v.status is MetricStatus.NOT_COMPUTED and v.code == "missing_input"
    assert v.details["missing"][0]["alias"] == "rev_ly"
    assert "rev_ly" in v.reason and v.value is None


def test_first_year_roe_needs_the_year_before_and_says_so(acme) -> None:
    v = value(acme, "roe", FY25)
    assert v.status is MetricStatus.NOT_COMPUTED and v.code == "missing_input"
    assert [m["alias"] for m in v.details["missing"]] == ["eq_ly"]
    assert v.details["missing"][0]["period_end"] == "2024-03-31"


def test_no_rows_for_an_instant_with_nothing_to_say(acme) -> None:
    """2024-03-31 holds only the opening cash: no balance-sheet metric row exists for it."""
    with transaction() as conn:
        rows = conn.execute(
            sa.select(tables.metric_values).where(tables.metric_values.c.period_end == "2024-03-31")
        ).all()
    assert rows == []


def test_sector_and_period_type_applicability(kit) -> None:
    kit.load_acme()
    kit.put("revenue", "2026-01-01", END26, 400, "Q")
    kit.put("profit_after_tax", "2026-01-01", END26, 40, "Q")
    run_metrics(kit.ISIN)
    q4 = kit.period("2026-01-01", END26, "Q")
    roe = get_metric(kit.ISIN, "roe", q4, BASIS)
    assert roe.code == "not_applicable" and "FY" in roe.reason
    nii = get_metric(kit.ISIN, "nii", q4, BASIS)  # a bank metric on a general company
    assert nii.code == "not_applicable" and "bank" in nii.reason.lower() or "general" in nii.reason
    assert get_metric(kit.ISIN, "cfo_to_pat", q4, BASIS).code == "not_applicable"


def test_division_by_zero_is_not_computed(kit) -> None:
    kit.load_acme()
    with transaction() as conn:
        conn.execute(
            sa.update(tables.canonical_facts)
            .where(
                tables.canonical_facts.c.concept == "revenue",
                tables.canonical_facts.c.period_end == END26,
            )
            .values(value="0")
        )
    run_metrics(kit.ISIN)
    v = value(kit, "operating_margin", FY26)
    assert v.status is MetricStatus.NOT_COMPUTED and v.code == "division_by_zero"


# --- acceptance 5: blocked inputs -----------------------------------------------------------------


def test_blocked_input_gives_not_computed_with_the_rule_id(kit) -> None:
    kit.load_acme()
    kit.result("bs_balances", "block", END26, "INSTANT", [])
    run_metrics(kit.ISIN)
    eq = value(kit, "equity_owners", END26)
    assert eq.status is MetricStatus.NOT_COMPUTED and eq.code == "blocked_by_validation"
    assert "bs_balances" in eq.reason
    assert eq.details["blocked_by"][0]["rule_ids"] == ["bs_balances"]
    roe = value(kit, "roe", FY26)  # inherits the block through its input metric
    assert roe.code == "blocked_by_validation" and "bs_balances" in roe.reason
    assert value(kit, "current_ratio", END26).status is MetricStatus.COMPUTED  # not blocked
    assert value(kit, "operating_margin", FY26).status is MetricStatus.COMPUTED


def test_a_block_on_the_opening_balance_blocks_an_averaging_metric(kit) -> None:
    kit.load_acme()
    kit.result("bs_balances", "block", END25, "INSTANT", [])
    run_metrics(kit.ISIN)
    assert value(kit, "roe", FY26).code == "blocked_by_validation"  # needs equity at 2025-03-31
    assert value(kit, "equity_owners", END26).status is MetricStatus.COMPUTED  # closing is fine
    assert value(kit, "dso", FY26).status is MetricStatus.COMPUTED


# --- acceptance 11: warnings ----------------------------------------------------------------------


def test_warning_is_returned_with_the_value_and_does_not_change_the_status(kit) -> None:
    kit.load_acme()
    kit.result("pl_profit_after_tax", "warn", END26, "FY", ["profit_after_tax"])
    run_metrics(kit.ISIN)
    v = value(kit, "net_margin", FY26)
    assert v.status is MetricStatus.COMPUTED
    assert v.warnings == [
        {"rule_id": "pl_profit_after_tax", "period_end": END26, "concept": "profit_after_tax"}
    ]
    assert "1 validation warning" in v.reason
    assert value(kit, "operating_margin", FY26).warnings == []  # PAT is not an input of it


# --- acceptance 10: basis change and corporate events ---------------------------------------------


def test_growth_across_an_accounting_policy_change_is_computed_and_flagged(kit) -> None:
    kit.load_acme()
    event = kit.event("accounting_policy_change", "2025-04-01")
    run_metrics(kit.ISIN)
    for metric in ("revenue_growth_yoy", "pat_growth_yoy", "operating_margin_change_yoy", "roe"):
        v = value(kit, metric, FY26)
        assert v.status is MetricStatus.COMPUTED, metric
        assert [e["event_id"] for e in v.basis_change] == [event]
        assert v.basis_change[0]["event_type"] == "accounting_policy_change"
        assert v.basis_change[0]["event_date"] == "2025-04-01"
        assert v.basis_change[0]["citation"] == "Note 5"
        assert "basis_change" not in v.reason  # the tokens are in details
        assert f"event {event}" in v.reason
    assert value(kit, "revenue_growth_yoy", FY26).value == D(1000) / D(800) - 1  # value unchanged


def test_a_comparative_restatement_flags_too(kit) -> None:
    kit.load_acme()
    event = kit.event("comparative_restatement", "2025-04-01")
    run_metrics(kit.ISIN)
    assert [e["event_id"] for e in value(kit, "pat_growth_yoy", FY26).basis_change] == [event]


def test_periods_on_the_same_side_of_the_event_are_not_flagged(kit) -> None:
    kit.load_acme()
    kit.event("accounting_policy_change", "2024-04-01")  # both FY25 and FY26 end after it
    run_metrics(kit.ISIN)
    assert value(kit, "revenue_growth_yoy", FY26).basis_change == []
    kit2_event = kit.event("accounting_policy_change", "2026-04-01")  # both end before it
    run_metrics(kit.ISIN)
    assert kit2_event and value(kit, "revenue_growth_yoy", FY26).basis_change == []


def test_a_single_period_metric_is_not_flagged(kit) -> None:
    kit.load_acme()
    kit.event("accounting_policy_change", "2025-04-01")
    run_metrics(kit.ISIN)
    assert value(kit, "operating_margin", FY26).basis_change == []
    assert value(kit, "current_ratio", END26).basis_change == []


def test_an_event_recorded_for_another_basis_is_ignored(kit) -> None:
    kit.load_acme()
    kit.event("accounting_policy_change", "2025-04-01", basis="standalone")
    run_metrics(kit.ISIN)
    assert value(kit, "revenue_growth_yoy", FY26).basis_change == []


def test_a_merger_between_the_periods_gives_corporate_event_break(kit) -> None:
    kit.load_acme()
    event = kit.event("merger", "2025-10-01")
    run_metrics(kit.ISIN)
    v = value(kit, "revenue_growth_yoy", FY26)
    assert v.status is MetricStatus.NOT_COMPUTED and v.code == "corporate_event_break"
    assert v.details["events"][0]["event_id"] == event
    assert value(kit, "operating_margin", FY26).status is MetricStatus.COMPUTED
    assert value(kit, "roe", FY26).code == "corporate_event_break"


# --- owners' profit: placeholder zero and fallbacks -----------------------------------------------


def _owners_case(kit, owners: str | None, pat: str, nci: str | None, derived: bool = False):
    start, end, kind = FY26
    kit.put("profit_after_tax", start, end, pat, kind)
    if owners is not None:
        kit.put("profit_attributable_owners", start, end, owners, kind, derived=derived)
    if nci is not None:
        kit.put("non_controlling_interest", end, end, nci)
    run_metrics(kit.ISIN)
    return value(kit, "profit_owners", FY26)


def test_owners_profit_zero_with_profit_and_no_minority_uses_total_profit(kit) -> None:
    """The stored 0 is hidden by the current view (a placeholder), so total profit stands in."""
    v = _owners_case(kit, owners="0", pat="110", nci=None)
    assert v.status is MetricStatus.COMPUTED and v.value == mn(110)
    assert v.details["fallbacks"][0]["used"] == "profit_after_tax"


def test_owners_profit_zero_with_zero_minority_interest_also_falls_back(kit) -> None:
    assert _owners_case(kit, owners="0", pat="110", nci="0").value == mn(110)


def test_owners_profit_zero_with_minority_interest_is_not_guessed(kit) -> None:
    v = _owners_case(kit, owners="0", pat="110", nci="50")
    assert v.status is MetricStatus.NOT_COMPUTED and v.code == "missing_input"
    assert v.details["missing"][0]["alias"] == "po"


def test_owners_profit_zero_is_genuine_when_profit_is_zero(kit) -> None:
    v = _owners_case(kit, owners="0", pat="0", nci="50")
    assert v.value == 0 and "placeholder_zero" not in v.details


def test_a_derived_owners_profit_is_not_trusted(kit) -> None:
    """H2 = FY - H1 inherits a placeholder H1: such a value is never used."""
    v = _owners_case(kit, owners="2522", pat="1212", nci="50", derived=True)
    assert v.code == "missing_input"
    assert v.details["placeholder_zero"][0]["why"] == "derived from a placeholder"


def test_reported_owners_profit_is_used_as_is(kit) -> None:
    v = _owners_case(kit, owners="100", pat="110", nci="50")
    assert v.value == mn(100) and "fallbacks" not in v.details


# --- restatements ---------------------------------------------------------------------------------


def test_flagged_restatement_of_an_input_is_linked_to_the_value(kit) -> None:
    kit.load_acme()
    start, end, kind = FY26
    old = kit.put("cfo", start, end, 300, kind, filing="FIL-old", filed_on="2026-04-01")
    new = kit.put(
        "cfo", start, end, 200, kind, filing="FIL-new", filed_on="2026-06-01", revision_no=1
    )
    with transaction() as conn:  # the load_acme cfo (200, FIL-x, 2026-05-01) sits between them
        conn.execute(
            sa.delete(tables.canonical_facts).where(
                tables.canonical_facts.c.concept == "cfo",
                tables.canonical_facts.c.source_filing_id == "FIL-x",
                tables.canonical_facts.c.period_end == END26,
            )
        )
    rid = kit.restate(old, new, "cfo", END26)
    unflagged = kit.restate(old, new, "cfo", END26, flagged=0)
    run_metrics(kit.ISIN)
    for metric in ("cfo_to_pat", "fcf"):
        v = value(kit, metric, FY26)
        assert [r["restatement_id"] for r in v.details["restatements"]] == [rid]
        assert v.details["restatements"][0]["old_value"] == str(mn(300))
        assert v.details["restatements"][0]["new_value"] == str(mn(200))
        assert "An input was restated: cfo." in v.reason
    assert unflagged != rid
    assert "restatements" not in value(kit, "operating_margin", FY26).details


# --- acceptance 7, 8, 9: versions, new metrics, determinism ---------------------------------------


def test_changing_a_formula_version_writes_new_rows_and_keeps_old_ones(acme) -> None:
    def bump(data):
        data["metrics"]["net_margin"]["version"] = "1.1"
        data["metrics"]["net_margin"]["formula"] = "pat / rev * 100"

    acme.edit_registry(bump)
    report = run_metrics(acme.ISIN)
    assert report.written >= 2  # net_margin at FY26 and FY25 (and any other period with facts)
    with transaction() as conn:
        rows = conn.execute(
            sa.select(tables.metric_values.c.formula_version, tables.metric_values.c.value)
            .where(
                tables.metric_values.c.metric_id == "net_margin",
                tables.metric_values.c.period_end == END26,
            )
            .order_by(tables.metric_values.c.metric_value_id)
        ).all()
    assert [r.formula_version for r in rows] == ["1.0", "1.1"]
    assert D(rows[0].value) == D(110) / D(1000)  # the old row is unchanged
    assert D(rows[1].value) == D(110) / D(1000) * 100
    assert value(acme, "net_margin", FY26).formula_version == "1.1"  # latest policy


def test_a_new_metric_in_the_registry_computes_without_code_changes(acme) -> None:
    def add(data):
        data["metrics"]["da_share_of_revenue"] = {
            "version": "1.0",
            "name": "D&A share of revenue",
            "description": "Depreciation over revenue.",
            "inputs": {
                "da": {"concept": "depreciation_amortisation"},
                "rev": {"concept": "revenue"},
            },
            "formula": "da / rev",
            "unit": "RATIO",
            "display": "percent",
            "period_types": ["FY"],
        }

    acme.edit_registry(add)
    run_metrics(acme.ISIN)
    assert value(acme, "da_share_of_revenue", FY26).value == D(50) / D(1000)


def test_results_are_identical_across_runs_and_unchanged_rows_are_not_rewritten(kit) -> None:
    kit.load_acme()
    first = run_metrics(kit.ISIN)
    second = run_metrics(kit.ISIN)
    assert first.written > 0 and first.unchanged == 0
    assert second.written == 0 and second.unchanged == first.written
    before = value(kit, "roe", FY26).value
    with transaction() as conn:
        conn.execute(sa.delete(tables.metric_values))
    run_metrics(kit.ISIN)
    assert value(kit, "roe", FY26).value == before


def test_a_changed_fact_writes_only_the_metrics_that_depend_on_it(kit) -> None:
    kit.load_acme()
    run_metrics(kit.ISIN)
    start, end, kind = FY26
    kit.put(
        "revenue", start, end, 1100, kind, filing="FIL-rev", filed_on="2026-07-01", revision_no=1
    )
    report = run_metrics(kit.ISIN)
    assert 0 < report.written < report.unchanged
    assert value(kit, "operating_margin", FY26).value == D(170) / D(1100)
    assert value(kit, "revenue_growth_yoy", FY26).value == D(1100) / D(800) - 1
    assert value(kit, "dso", FY26).value == D(150) / D(1100) * D(365)
    with transaction() as conn:
        count = conn.execute(
            sa.select(sa.func.count())
            .select_from(tables.metric_values)
            .where(
                tables.metric_values.c.metric_id == "operating_margin",
                tables.metric_values.c.period_end == END26,
            )
        ).scalar()
    assert count == 2  # the old row is still there


def test_run_can_be_limited_to_periods_and_to_a_basis(kit) -> None:
    kit.load_acme()
    only = kit.period(*FY26)
    report = run_metrics(kit.ISIN, periods=[only], basis=BASIS)
    assert report.written > 0
    assert value(kit, "operating_margin", FY26) is not None
    assert value(kit, "operating_margin", FY25) is None
    assert run_metrics(kit.ISIN, basis="standalone").written == 0  # no standalone facts


def test_unknown_company_is_refused(kit) -> None:
    with pytest.raises(KeyError, match="unknown company"):
        run_metrics("INE999A01010")


def test_a_run_is_recorded_as_a_job(kit) -> None:
    kit.load_acme()
    report = run_metrics(kit.ISIN)
    with transaction() as conn:
        job = conn.execute(
            sa.select(tables.jobs).where(tables.jobs.c.job_type == "metrics_run")
        ).one()
    assert job.status == "succeeded" and job.isin == kit.ISIN
    assert report.run_id in job.stats_json
