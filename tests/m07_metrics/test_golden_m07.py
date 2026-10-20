"""Checks against the real database (skipped until it is populated).

The real data/finrag.db is never written: it is copied to a temp folder, migrated, and the metrics
are computed on the copy. Every expected value is computed here, by hand, from the literal input
facts (which are first checked against the real canonical_facts); the engine must match exactly.
"""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from finrag.core.db import _make_engine, init_db, tables, transaction
from finrag.core.types import MetricStatus, Period, PeriodType
from finrag.facts.current_view import get_fact, get_facts, load_facts
from finrag.metrics.engine import run_metrics
from finrag.metrics.query import get_metric

D = Decimal
REAL = Path(__file__).resolve().parents[2]
DB = REAL / "data" / "finrag.db"
GABRIEL, NETWEB, HDFC = "INE524A01029", "INE0NT901020", "INE040A01034"
CONS, STAND = "consolidated", "standalone"


def _has_real_data() -> bool:
    if not DB.exists():
        return False
    engine = sa.create_engine(f"sqlite:///file:{DB.as_posix()}?mode=ro&uri=true")
    try:
        with engine.connect() as conn:
            isins = {
                r[0] for r in conn.execute(sa.select(tables.canonical_facts.c.isin).distinct())
            }
            events = conn.execute(sa.select(sa.func.count()).select_from(tables.corporate_events))
            return {GABRIEL, NETWEB, HDFC} <= isins and bool(events.scalar())
    except sa.exc.OperationalError:
        return False
    finally:
        engine.dispose()


pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(not _has_real_data(), reason="real facts/events not populated"),
]


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    """A migrated copy of the real database with the metrics of all three companies computed."""
    copy = tmp_path_factory.mktemp("m07") / "finrag.db"
    shutil.copy(DB, copy)
    engine = _make_engine(copy)
    init_db(engine)
    for isin in (GABRIEL, NETWEB, HDFC):
        run_metrics(isin, engine=engine)
    yield engine
    engine.dispose()


def fy(year: int) -> Period:
    return Period(date(year - 1, 4, 1), date(year, 3, 31), PeriodType.FY)


def q(start: date, end: date) -> Period:
    return Period(start, end, PeriodType.Q)


def at(day: date) -> Period:
    return Period(day, day, PeriodType.INSTANT)


FY26, FY25 = fy(2026), fy(2025)
Q4_26 = q(date(2026, 1, 1), date(2026, 3, 31))
Q4_25 = q(date(2025, 1, 1), date(2025, 3, 31))
Q1_27 = q(date(2026, 4, 1), date(2026, 6, 30))
END26, END25 = at(date(2026, 3, 31)), at(date(2025, 3, 31))


def metric(eng, isin, metric_id, period, basis=CONS):
    value = get_metric(isin, metric_id, period, basis, eng)
    assert value is not None, f"{metric_id} {period} was not stored"
    return value


def facts_are(eng, isin, period, basis, expected: dict[str, int]) -> None:
    """The literal inputs of a hand calculation equal the real current facts."""
    for concept, rupees in expected.items():
        found = get_fact(isin, concept, period, basis, engine=eng)
        assert found is not None and found.value == D(rupees), f"{concept} {period.end}"


# --- Gabriel: every starter metric by hand --------------------------------------------------------

G26 = dict(  # FY26 consolidated, rupees
    revenue=46_669_330_000, profit_before_tax=3_361_520_000, finance_cost=141_770_000,
    other_income=263_060_000, exceptional_items=-138_130_000,
    depreciation_amortisation=1_001_090_000,
    profit_after_tax=2_521_640_000, profit_attributable_owners=2_522_760_000,
    cfo=3_452_740_000, capex_ppe=1_684_310_000, capex_intangibles=309_740_000,
)  # fmt: skip
G25 = dict(
    revenue=40_633_810_000, profit_before_tax=3_241_640_000, finance_cost=101_790_000,
    other_income=259_600_000, exceptional_items=0, profit_after_tax=2_449_810_000,
    depreciation_amortisation=812_830_000,
)  # fmt: skip
G_BS26 = dict(
    total_equity=13_918_230_000, non_controlling_interest=230_780_000,
    long_term_borrowings=992_200_000, short_term_borrowings=488_970_000,
    total_current_assets=14_841_850_000, total_current_liabilities=8_921_650_000,
    trade_receivables_current=6_765_070_000, trade_receivables_noncurrent=0,
)  # fmt: skip
G_BS25 = dict(
    total_equity=11_832_850_000, non_controlling_interest=0, long_term_borrowings=0,
    short_term_borrowings=238_650_000,
)  # fmt: skip


def test_gabriel_inputs_are_the_real_facts(eng) -> None:
    facts_are(eng, GABRIEL, FY26, CONS, G26)
    facts_are(eng, GABRIEL, FY25, CONS, G25)
    facts_are(eng, GABRIEL, END26, CONS, G_BS26)
    facts_are(eng, GABRIEL, END25, CONS, G_BS25)


def test_gabriel_fy26_metrics_match_hand_values(eng) -> None:
    g, p = G26, G25
    op = g["profit_before_tax"] + g["finance_cost"] - g["other_income"] - g["exceptional_items"]
    op_ly = p["profit_before_tax"] + p["finance_cost"] - p["other_income"] - p["exceptional_items"]
    assert op == 3_378_360_000 and op_ly == 3_083_830_000
    eq, eq_ly = 13_918_230_000 - 230_780_000, 11_832_850_000
    ce = 13_918_230_000 + 992_200_000 + 488_970_000
    ce_ly = 11_832_850_000 + 0 + 238_650_000
    expected = {
        "operating_profit": D(op),
        "ebitda": D(op + g["depreciation_amortisation"]),
        "operating_margin": D(op) / D(g["revenue"]),
        "ebitda_margin": D(op + g["depreciation_amortisation"]) / D(g["revenue"]),
        "net_margin": D(g["profit_after_tax"]) / D(g["revenue"]),
        "revenue_growth_yoy": D(g["revenue"]) / D(p["revenue"]) - 1,
        "pat_growth_yoy": (D(g["profit_after_tax"]) - D(p["profit_after_tax"]))
        / abs(D(p["profit_after_tax"])),
        "operating_margin_change_yoy": D(op) / D(g["revenue"]) - D(op_ly) / D(p["revenue"]),
        "associates_share_of_pat": D(0) / D(g["profit_after_tax"]),
        "profit_owners": D(g["profit_attributable_owners"]),
        "roe": D(g["profit_attributable_owners"]) / ((D(eq) + D(eq_ly)) / 2),
        "roce": D(op) / ((D(ce) + D(ce_ly)) / 2),
        "cfo_to_pat": D(g["cfo"]) / D(g["profit_after_tax"]),
        "fcf": D(g["cfo"] - g["capex_ppe"] - g["capex_intangibles"]),
        "dso": D(6_765_070_000 + 0) / D(g["revenue"]) * D(365),
    }
    for metric_id, want in expected.items():
        got = metric(eng, GABRIEL, metric_id, FY26)
        assert got.status is MetricStatus.COMPUTED, (metric_id, got.reason)
        assert got.value == want, metric_id
    assert expected["fcf"] == 1_458_690_000


def test_gabriel_balance_sheet_metrics_match_hand_values(eng) -> None:
    owners = D(13_918_230_000 - 230_780_000)
    assert metric(eng, GABRIEL, "equity_owners", END26).value == owners
    assert metric(eng, GABRIEL, "capital_employed", END26).value == D(15_399_400_000)
    assert (
        metric(eng, GABRIEL, "debt_to_equity", END26).value == D(992_200_000 + 488_970_000) / owners
    )
    assert metric(eng, GABRIEL, "current_ratio", END26).value == D(14_841_850_000) / D(
        8_921_650_000
    )


def test_gabriel_revision_is_used_and_the_restatement_is_linked(eng) -> None:
    """FY26 operating CF is the revised 3,452.74 mn, not the original 4,435.59 mn."""
    current = get_fact(GABRIEL, "cfo", FY26, CONS, engine=eng)
    assert current.source_filing_id == "FIL-d656339b02dc" and current.revision_no == 1
    for metric_id in ("fcf", "cfo_to_pat"):
        got = metric(eng, GABRIEL, metric_id, FY26)
        cfo = [r for r in got.details["restatements"] if r["concept"] == "cfo"]
        assert cfo[0]["new_value"] == "3452740000" and cfo[0]["old_value"] == "4435590000"
        assert got.reason.count("restated") == 1 and "cfo" in got.reason
    fcf = metric(eng, GABRIEL, "fcf", FY26)
    assert {r["concept"] for r in fcf.details["restatements"]} == {
        "cfo", "capex_ppe", "capex_intangibles",
    }  # fmt: skip
    # intangibles capex 1,029.90 -> 309.74 mn
    intang = [r for r in fcf.details["restatements"] if r["concept"] == "capex_intangibles"][0]
    assert (intang["old_value"], intang["new_value"]) == ("1029900000", "309740000")
    assert metric(eng, GABRIEL, "fcf", FY26).value == D(3_452_740_000 - 1_684_310_000 - 309_740_000)
    assert metric(eng, GABRIEL, "cfo_to_pat", FY26).value != D(4_435_590_000) / D(2_521_640_000)


def test_gabriel_assumed_zeros_and_lease_gap_are_recorded(eng) -> None:
    ce = metric(eng, GABRIEL, "capital_employed", END26)
    assert [z["concept"] for z in ce.details["assumed_zero"]] == ["lease_liabilities"]
    roce = metric(eng, GABRIEL, "roce", FY26)
    assert {z["period_end"] for z in roce.details["assumed_zero"]} == {"2025-03-31", "2026-03-31"}
    assert any("Lease liabilities" in text for text in _limitations("roce"))
    assert any("Lease liabilities" in text for text in _limitations("capital_employed"))


def _limitations(metric_id: str) -> list[str]:
    from finrag.metrics.registry import load_metrics

    return list(load_metrics().get(metric_id).limitations)


def test_gabriel_associates_share_of_pat(eng) -> None:
    got = metric(eng, GABRIEL, "associates_share_of_pat", Q1_27)
    assert got.value == D(427_430_000) / D(1_081_260_000)
    assert abs(got.value - D("0.395")) < D("0.0005")  # about 39.5 %
    earlier = [
        v
        for f in get_facts(GABRIEL, ["share_of_associates_jv"], None, CONS, engine=eng)
        if f.period.end != Q1_27.end and f.period.type is not PeriodType.INSTANT
        for v in [get_metric(GABRIEL, "associates_share_of_pat", f.period, CONS, eng)]
    ]
    assert earlier and all(v.value == 0 for v in earlier)


# --- a bad OCR column is rejected, not used ------------------------------------------------------


def test_netweb_q1_fy26_ocr_column_with_profit_100x_off_never_beats_the_xbrl(eng) -> None:
    """Q2 FY26 results scan, quarter to 30 Jun 2025: profit read as 30,479 (it is 304.79).

    Profit before tax 416.06 less tax 111.27 is 304.79. Before the Q1 FY26 XBRL was added (inbox,
    2026-10-20) there was no Q1 FY26 filing and the gap-fill gate had to reject the whole column
    so that 30,479 mn was not the current profit. The XBRL now supplies 304.79 mn, which confirms
    the gate's verdict (the OCR value is exactly 100x), and the scan stays stored but never current.
    """
    period = q(date(2025, 4, 1), date(2025, 6, 30))
    pat_now = get_fact(NETWEB, "profit_after_tax", period, STAND, engine=eng)
    assert pat_now.value == D("304790000") and pat_now.source_class.value == "xbrl"
    pbt_now = get_fact(NETWEB, "profit_before_tax", period, STAND, engine=eng)
    assert pbt_now.source_class.value == "xbrl"
    with transaction(eng) as conn:
        stored = {
            (f.concept, f.source_class.value): f
            for f in load_facts(conn, NETWEB, None, STAND, [period], include_rejected=True)
        }
    ocr = stored[("profit_after_tax", "ocr")]
    assert ocr.value == D("30479000000") and ocr.gap_rejected and ocr.confidence <= 0.3
    assert ocr.value / pat_now.value == 100  # a decimal slip, confirmed by the filed number
    assert stored[("profit_before_tax", "ocr")].value - stored[("tax_expense", "ocr")].value == D(
        "304790000.00"
    )
    margin = get_metric(NETWEB, "net_margin", period, STAND, eng)
    assert margin.status is MetricStatus.COMPUTED and D(0) < margin.value < D("0.3")  # not 30x
    # the half year of the same PDF is XBRL-covered (Q2 FY26 filing) and untouched
    h1 = get_fact(
        NETWEB,
        "profit_after_tax",
        Period(date(2025, 4, 1), date(2025, 9, 30), PeriodType.H),
        STAND,
        engine=eng,
    )
    assert h1 is not None and h1.value == D("619120000") and h1.source_class.value == "xbrl"


# --- operating profit and associates, per company -------------------------------------------------


@pytest.mark.parametrize(("isin", "basis"), [(GABRIEL, CONS), (NETWEB, STAND)])
def test_operating_profit_excludes_associates_and_ties_to_expenses(eng, isin, basis) -> None:
    """PBT - tax + associates + discontinued = PAT: PBT excludes associates, nothing to remove."""
    facts = {
        (f.concept, f.period.start, f.period.end): f.value
        for f in get_facts(isin, None, None, basis, engine=eng)
    }
    checked = 0
    for (concept, start, end), pat in facts.items():
        if concept != "profit_after_tax":
            continue
        pbt, tax = (
            facts.get(("profit_before_tax", start, end)),
            facts.get(("tax_expense", start, end)),
        )
        if pbt is None or tax is None:
            continue
        assoc = facts.get(("share_of_associates_jv", start, end), D(0))
        disc = facts.get(("profit_discontinued_ops", start, end), D(0))
        assert pbt - tax + assoc + disc == pat, (isin, start, end)
        fin = facts.get(("finance_cost", start, end))
        oi = facts.get(("other_income", start, end))
        rev = facts.get(("revenue", start, end))
        spend = facts.get(("total_expenses", start, end))
        if None in (fin, oi, rev, spend):
            continue
        period = Period(start, end, PeriodType(_ptype(isin, basis, start, end, eng)))
        exc = facts.get(("exceptional_items", start, end), D(0))
        got = get_metric(isin, "operating_profit", period, basis, eng)
        assert got.value == pbt + fin - oi - exc == rev - (spend - fin), (isin, start, end)
        checked += 1
    assert checked >= 5


def _ptype(isin, basis, start, end, eng) -> str:
    for f in get_facts(isin, ["profit_after_tax"], None, basis, engine=eng):
        if (f.period.start, f.period.end) == (start, end):
            return f.period.type.value
    raise AssertionError("period not found")  # pragma: no cover


def test_gabriel_q1_fy27_pbt_excludes_the_associates(eng) -> None:
    pbt = get_fact(GABRIEL, "profit_before_tax", Q1_27, CONS, engine=eng).value
    tax = get_fact(GABRIEL, "tax_expense", Q1_27, CONS, engine=eng).value
    assoc = get_fact(GABRIEL, "share_of_associates_jv", Q1_27, CONS, engine=eng).value
    pat = get_fact(GABRIEL, "profit_after_tax", Q1_27, CONS, engine=eng).value
    assert (pbt - tax, assoc, pat) == (D(653_830_000), D(427_430_000), D(1_081_260_000))
    assert metric(eng, GABRIEL, "operating_profit", Q1_27).value == D(
        918_640_000
    )  # 905.17+59.76-46.29


def test_hdfc_has_no_associates_above_or_below_pbt(eng) -> None:
    for basis in (CONS, STAND):
        facts = {
            (f.concept, f.period.start, f.period.end): f.value
            for f in get_facts(HDFC, None, None, basis, engine=eng)
        }
        for (concept, start, end), pat in facts.items():
            if concept == "profit_after_tax" and ("tax_expense", start, end) in facts:
                pbt, tax = (
                    facts[("profit_before_tax", start, end)],
                    facts[("tax_expense", start, end)],
                )
                assert pbt - tax + facts.get(("share_of_associates_jv", start, end), D(0)) == pat


# --- Netweb: basis change -------------------------------------------------------------------------

N26 = dict(
    revenue=21_835_630_000, profit_before_tax=2_765_200_000, finance_cost=129_530_000,
    other_income=188_420_000, profit_after_tax=2_058_160_000,
)  # fmt: skip
N25 = dict(revenue=11_490_210_000, profit_after_tax=1_144_750_000)


def test_netweb_fy26_vs_fy25_is_computed_and_flagged_with_the_recorded_event(eng) -> None:
    facts_are(eng, NETWEB, FY26, STAND, N26)
    facts_are(eng, NETWEB, FY25, STAND, N25)
    for metric_id, want in (
        ("revenue_growth_yoy", D(N26["revenue"]) / D(N25["revenue"]) - 1),
        (
            "pat_growth_yoy",
            (D(N26["profit_after_tax"]) - D(N25["profit_after_tax"])) / D(N25["profit_after_tax"]),
        ),
    ):
        got = metric(eng, NETWEB, metric_id, FY26, STAND)
        assert got.status is MetricStatus.COMPUTED and got.value == want
        assert [(e["event_id"], e["event_type"], e["event_date"]) for e in got.basis_change] == [
            (1, "accounting_policy_change", "2025-04-01")
        ]
        assert "Note 5" in got.basis_change[0]["citation"]
    for metric_id in ("operating_margin_change_yoy", "roe", "roce"):
        got = metric(eng, NETWEB, metric_id, FY26, STAND)
        assert got.status is MetricStatus.COMPUTED and [
            e["event_id"] for e in got.basis_change
        ] == [1]


def test_netweb_single_period_values_are_not_flagged(eng) -> None:
    for metric_id in ("operating_margin", "net_margin", "dso", "fcf", "cfo_to_pat"):
        assert metric(eng, NETWEB, metric_id, FY26, STAND).basis_change == []
    assert metric(eng, NETWEB, "current_ratio", END26, STAND).basis_change == []


def test_netweb_quarter_on_quarter_across_the_event_is_flagged_too(eng) -> None:
    got = metric(eng, NETWEB, "revenue_growth_yoy", Q4_26, STAND)
    assert got.status is MetricStatus.COMPUTED and [e["event_id"] for e in got.basis_change] == [1]


def test_netweb_roe_carries_the_warning_and_the_owners_fallback(eng) -> None:
    got = metric(eng, NETWEB, "roe", FY26, STAND)
    assert {(w["rule_id"], w["period_end"], w["concept"]) for w in got.warnings} == {
        ("xbrl_vs_pdf", "2025-03-31", "total_equity")
    }
    assert got.details["fallbacks"][0]["used"] == "profit_after_tax"
    eq, eq_ly = D(7_232_980_000), D(5_303_320_000)
    assert got.value == D(N26["profit_after_tax"]) / ((eq + eq_ly) / 2)


def test_netweb_fy25_comparison_has_no_fy24_to_compare_with(eng) -> None:
    got = metric(eng, NETWEB, "revenue_growth_yoy", FY25, STAND)
    assert got.status is MetricStatus.NOT_COMPUTED and got.code == "missing_input"
    assert got.details["missing"][0]["alias"] == "rev_ly"


# --- HDFC Bank ------------------------------------------------------------------------------------


def test_hdfc_company_metrics_are_not_applicable(eng) -> None:
    for metric_id, period in (
        ("operating_margin", FY26),
        ("dso", FY26),
        ("roe", FY26),
        ("current_ratio", END26),
        ("debt_to_equity", END26),
    ):
        got = metric(eng, HDFC, metric_id, period)
        assert got.status is MetricStatus.NOT_COMPUTED and got.code == "not_applicable"


def test_hdfc_bank_equity_is_capital_plus_reserves(eng) -> None:
    facts_are(
        eng,
        HDFC,
        END26,
        CONS,
        {"bank_share_capital": 15_393_400_000, "bank_reserves_and_surplus": 5_799_750_200_000},
    )
    got = metric(eng, HDFC, "bank_equity", END26)
    assert got.value == D(15_393_400_000 + 5_799_750_200_000) == D(5_815_143_600_000)


def test_hdfc_bank_metrics_match_hand_values(eng) -> None:
    ie = get_fact(HDFC, "bank_interest_earned", FY26, CONS, engine=eng).value
    ix = get_fact(HDFC, "bank_interest_expended", FY26, CONS, engine=eng).value
    facts_are(eng, HDFC, FY26, CONS, {
        "other_income": 1_468_476_600_000, "bank_operating_expenses": 1_811_739_100_000,
        "bank_provisions": 266_562_200_000, "profit_attributable_owners": 760_259_700_000,
        "profit_after_tax": 760_259_700_000,
    })  # fmt: skip
    facts_are(eng, HDFC, FY25, CONS, {"profit_after_tax": 707_922_500_000})
    facts_are(
        eng,
        HDFC,
        END26,
        CONS,
        {"bank_advances": 30_507_832_300_000, "bank_deposits": 30_996_382_900_000},
    )
    facts_are(
        eng,
        HDFC,
        END25,
        CONS,
        {"bank_advances": 27_249_381_600_000, "bank_deposits": 27_108_982_300_000},
    )
    nii = ie - ix
    assert nii == D(1_631_239_200_000)
    expected = {
        ("nii", FY26): nii,
        ("cost_to_income", FY26): D(1_811_739_100_000) / (nii + D(1_468_476_600_000)),
        ("credit_cost", FY26): D(266_562_200_000)
        / ((D(30_507_832_300_000) + D(27_249_381_600_000)) / 2),
        ("advances_growth_yoy", END26): D(30_507_832_300_000) / D(27_249_381_600_000) - 1,
        ("deposits_growth_yoy", END26): D(30_996_382_900_000) / D(27_108_982_300_000) - 1,
        ("credit_deposit_ratio", END26): D(30_507_832_300_000) / D(30_996_382_900_000),
        ("roe_bank", FY26): D(760_259_700_000)
        / ((D(5_815_143_600_000) + D(5_179_842_000_000)) / 2),
        ("pat_growth_yoy", FY26): (D(760_259_700_000) - D(707_922_500_000)) / D(707_922_500_000),
    }
    for (metric_id, period), want in expected.items():
        got = metric(eng, HDFC, metric_id, period)
        assert got.status is MetricStatus.COMPUTED, (metric_id, got.reason)
        assert got.value == want, metric_id
    # net interest income + other income - operating expenses = the bank's own operating profit
    reported = get_fact(
        HDFC, "bank_operating_profit_before_provisions", FY26, CONS, engine=eng
    ).value
    assert nii + D(1_468_476_600_000) - D(1_811_739_100_000) == reported


def test_hdfc_npa_and_cet1_fall_back_to_standalone_and_say_so(eng) -> None:
    for metric_id in ("gross_npa", "net_npa", "gross_npa_ratio", "net_npa_ratio", "cet1_ratio"):
        got = metric(eng, HDFC, metric_id, Q1_27)
        assert got.status is MetricStatus.COMPUTED
        assert got.basis == CONS and got.basis_used == STAND
        assert got.details["basis_fallback"] == STAND and "standalone" in got.reason
    assert metric(eng, HDFC, "gross_npa", Q1_27).value == D(358_463_500_000)
    assert metric(eng, HDFC, "net_npa", Q1_27).value == D(123_572_700_000)
    assert metric(eng, HDFC, "cet1_ratio", Q1_27).value == D("0.1957")


def test_hdfc_gross_npa_ratio_four_quarter_trend(eng) -> None:
    trend = [
        (q(date(2025, 1, 1), date(2025, 3, 31)), "0.0133"),
        (q(date(2025, 7, 1), date(2025, 9, 30)), "0.0124"),
        (Q4_26, "0.0115"),
        (Q1_27, "0.0117"),
    ]
    for period, want in trend:
        got = metric(eng, HDFC, "gross_npa_ratio", period)
        assert got.value == D(want) and got.details["basis_fallback"] == STAND
        stand = metric(eng, HDFC, "gross_npa_ratio", period, STAND)
        assert stand.value == D(want) and "basis_fallback" not in stand.details
    # reported, not recomputed: gross NPA / balance-sheet advances would give 1.16 %
    recomputed = D(340_611_900_000) / D(29_371_662_600_000)
    assert round(recomputed, 4) == D("0.0116") != metric(eng, HDFC, "gross_npa_ratio", Q4_26).value
    assert any("1.16" in text and "1.15" in text for text in _limitations("gross_npa_ratio"))


def test_first_year_roe_is_not_computed_for_every_company(eng) -> None:
    for isin, metric_id, basis in (
        (GABRIEL, "roe", CONS),
        (NETWEB, "roe", STAND),
        (HDFC, "roe_bank", CONS),
    ):
        got = metric(eng, isin, metric_id, FY25, basis)
        assert got.status is MetricStatus.NOT_COMPUTED and got.code == "missing_input"
        assert got.details["missing"][0]["alias"] == "eq_ly"
        assert got.details["missing"][0]["period_end"] == "2024-03-31"
        assert got.value is None


def test_gabriel_placeholder_owners_profit_is_hidden_and_h2_is_not_invented(eng) -> None:
    """Owners' profit tagged 0 beside a non-zero profit is not a fact; the derived H2 is gone.

    H1 FY26 owners' profit is not disclosed (tag 0), so H2 = FY - H1 cannot be derived and the
    wrong 2,522.76 mn (the whole year) is no longer current. Nothing is guessed in its place.
    """
    h2 = Period(date(2025, 10, 1), date(2026, 3, 31), PeriodType.H)
    h1 = Period(date(2025, 4, 1), date(2025, 9, 30), PeriodType.H)
    for period in (h1, h2, FY25):
        assert get_fact(GABRIEL, "profit_attributable_owners", period, CONS, engine=eng) is None
    assert get_fact(GABRIEL, "profit_attributable_owners", FY26, CONS, engine=eng).value == D(
        2_522_760_000
    )
    assert get_fact(GABRIEL, "profit_after_tax", h2, CONS, engine=eng).value == D(1_211_640_000)
    got = metric(eng, GABRIEL, "profit_owners", h2)
    assert got.status is MetricStatus.NOT_COMPUTED and got.code == "missing_input"
    # H1 and FY25 had no minority interest, so total profit stands in for the untagged figure
    assert metric(eng, GABRIEL, "profit_owners", h1).value == D(1_310_000_000)
    assert metric(eng, GABRIEL, "profit_owners", FY25).value == D(2_449_810_000)


def test_hdfc_cost_to_income_prefers_standalone_and_both_are_computed(eng) -> None:
    """FY26: 38.00 % standalone, 58.45 % consolidated (consolidated opex includes subsidiaries)."""
    from finrag.metrics.registry import load_metrics

    assert load_metrics().get("cost_to_income").preferred_basis == "standalone"
    facts_are(
        eng,
        HDFC,
        FY26,
        STAND,
        {"other_income": 625_325_700_000, "bank_operating_expenses": 726_603_300_000},
    )
    ie = get_fact(HDFC, "bank_interest_earned", FY26, STAND, engine=eng).value
    ix = get_fact(HDFC, "bank_interest_expended", FY26, STAND, engine=eng).value
    nii = ie - ix
    assert nii == D(1_286_860_400_000)
    want = D(726_603_300_000) / (nii + D(625_325_700_000))
    got = metric(eng, HDFC, "cost_to_income", FY26, STAND)
    assert got.value == want
    assert round(got.value * 100, 2) == D("38.00")
    consolidated = metric(eng, HDFC, "cost_to_income", FY26, CONS)
    assert round(consolidated.value * 100, 2) == D("58.45")
    assert metric(eng, HDFC, "cost_to_income", FY26, STAND).basis_used == STAND


# --- Gabriel's growth over the restated Q1 FY26 --------------------------------------------------

Q1_27 = q(date(2026, 4, 1), date(2026, 6, 30))


@pytest.mark.parametrize(
    ("basis", "headline", "restated"),
    [(CONS, D("1081.26") / D("619.72") - 1, D("1081.26") / D("1054.53") - 1),
     (STAND, D("759.68") / D("556.71") - 1, D("759.68") / D("596.13") - 1)],
)  # fmt: skip
def test_gabriel_q1_fy27_profit_growth_is_shown_on_both_bases_with_the_scheme_named(
    eng, basis, headline, restated
) -> None:
    """The headline is against the profit first filed for Q1 FY26 (619.72 mn consolidated, 556.71
    standalone); the Q1 FY27 results print Q1 FY26 again after the Scheme (1,054.53 and 596.13)."""
    v = get_metric(GABRIEL, "pat_growth_yoy", Q1_27, basis, eng)
    assert v.status is MetricStatus.COMPUTED and abs(v.value - headline) < D("1e-20")
    found = v.details["like_for_like"]
    assert found["headline_like_for_like"] is False
    assert abs(D(found["restated_value"]) - restated) < D("1e-20")
    assert found["event"]["event_type"] == "comparative_restatement"
    assert found["event"]["event_date"] == "2026-04-01"
    assert "Scheme of Arrangement" in found["event"]["citation"]
    (item,) = found["inputs"]
    assert (item["period"], item["restated_filing_id"]) == ("Q1 FY26", "FIL-83a2f50f0a6d")
    assert item["restated_page"] == (7 if basis == CONS else 4)
    assert "Not like for like" in v.reason


def test_growth_not_over_the_restated_period_has_no_like_for_like_figure(eng) -> None:
    """Q4 FY26 against Q4 FY25 is not touched by the Scheme's comparatives."""
    q4_26 = q(date(2026, 1, 1), date(2026, 3, 31))
    v = get_metric(GABRIEL, "pat_growth_yoy", q4_26, CONS, eng)
    assert v is None or "like_for_like" not in v.details
