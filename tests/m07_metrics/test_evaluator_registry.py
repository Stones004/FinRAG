"""The formula language and the registry's load-time checks (M07 acceptance 2, 3, 4)."""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from finrag.metrics.evaluator import EvalError, FormulaError, compile_formula, evaluate
from finrag.metrics.registry import MetricsError, load_metrics, parse_registry
from finrag.normalize.taxonomy import parse_taxonomy

D = Decimal
REAL = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def taxonomy():
    return parse_taxonomy(yaml.safe_load((REAL / "config" / "taxonomy.yaml").read_text("utf-8")))


@pytest.fixture(scope="module")
def raw_registry():
    return yaml.safe_load((REAL / "config" / "metrics.yaml").read_text("utf-8"))


# --- evaluator ----------------------------------------------------------------------------------


def test_arithmetic_precedence_and_unary_minus() -> None:
    values = {"a": D("10"), "b": D("4"), "c": D("2")}
    assert evaluate("a + b * c", values) == D("18")
    assert evaluate("(a + b) * c", values) == D("28")
    assert evaluate("-a + b", values) == D("-6")
    assert evaluate("a / b - 1", values) == D("1.5")
    assert evaluate("a / c / c", values) == D("2.5")


def test_functions() -> None:
    values = {"a": D("-3"), "b": D("5")}
    assert evaluate("avg(a, b)", values) == D("1")
    assert evaluate("abs(a)", values) == D("3")
    assert evaluate("min(a, b)", values) == D("-3")
    assert evaluate("max(a, b, 9)", values) == D("9")


def test_decimal_exactness_no_float() -> None:
    assert evaluate("0.1 + 0.2", {}) == D("0.3")  # a float would give 0.30000000000000004
    assert evaluate("a / b", {"a": D(1), "b": D(3)}, precision=28) == D(1) / D(3)


def test_precision_comes_from_settings() -> None:
    long = evaluate("a / b", {"a": D(1), "b": D(3)})
    assert len(long.as_tuple().digits) == 28
    assert len(evaluate("a / b", {"a": D(1), "b": D(3)}, precision=10).as_tuple().digits) == 10


def test_division_by_zero_is_an_eval_error() -> None:
    with pytest.raises(EvalError) as caught:
        evaluate("a / b", {"a": D(1), "b": D(0)})
    assert caught.value.code == "division_by_zero"
    with pytest.raises(EvalError) as zero_over_zero:
        evaluate("a / b", {"a": D(0), "b": D(0)})
    assert zero_over_zero.value.code == "division_by_zero"


@pytest.mark.parametrize(
    "text",
    [
        "__import__('os')",
        "__import__('os').system('x')",
        "a.real",
        "a ** 2",
        "a % 2",
        "a // 2",
        "a if b else c",
        "a < b",
        "lambda: 1",
        "[a, b]",
        "'text'",
        "True",
        "abs(a=1)",
        "abs()",
        "abs(a, b)",
        "avg(a)",
        "open('x')",
        "eval('1')",
        "a[0]",
        "not a",
        "+a",
        "avg",
        "a and b",
    ],
)
def test_evaluator_refuses_anything_outside_the_language(text: str) -> None:
    with pytest.raises(FormulaError):
        evaluate(text, {"a": D(1), "b": D(2), "c": D(3)})


def test_unknown_name_is_refused() -> None:
    with pytest.raises(FormulaError, match="no value"):
        evaluate("a + zzz", {"a": D(1)})
    assert compile_formula("a + zzz").names == {"a", "zzz"}


# --- registry ------------------------------------------------------------------------------------


def test_real_registry_loads_and_orders_inputs_first(taxonomy) -> None:
    registry = load_metrics(taxonomy)
    order = registry.order()
    assert len(order) == len(set(order)) >= 30
    for metric_id in order:
        for spec in registry.get(metric_id).inputs.values():
            if spec.metric:
                assert order.index(spec.metric) < order.index(metric_id)
    assert {
        "operating_profit", "ebitda", "operating_margin", "ebitda_margin", "net_margin",
        "revenue_growth_yoy", "pat_growth_yoy", "operating_margin_change_yoy",
        "associates_share_of_pat", "equity_owners", "bank_equity", "capital_employed", "roe",
        "roce", "debt_to_equity", "current_ratio", "cfo_to_pat", "fcf", "dso", "roe_bank", "nii",
        "cost_to_income", "credit_cost", "advances_growth_yoy", "deposits_growth_yoy",
        "credit_deposit_ratio", "gross_npa", "net_npa", "gross_npa_ratio", "net_npa_ratio",
        "cet1_ratio",
    } <= set(order)  # fmt: skip


def test_synonyms_may_be_shared_across_sectors_and_describe_has_the_caveats(taxonomy) -> None:
    registry = load_metrics(taxonomy)
    assert registry.synonyms()["roe"] == ["roe", "roe_bank"]
    assert "equity_owners" not in {m for ids in registry.synonyms().values() for m in ids}  # helper
    described = registry.describe("capital_employed")
    assert any("lease" in text.lower() for text in described["limitations"])
    assert described["formula"] == "te + ltb + stb + ll"
    assert "lease_liabilities" in {i.get("concept") for i in described["inputs"].values()}
    assert any("1.15" in text for text in registry.describe("gross_npa_ratio")["limitations"])
    with pytest.raises(KeyError, match="unknown metric"):
        registry.get("nope")


def _mutate(raw, taxonomy, change) -> MetricsError:
    data = copy.deepcopy(raw)
    change(data["metrics"])
    with pytest.raises(MetricsError) as caught:
        parse_registry(data, taxonomy)
    return caught.value


def test_dependency_cycle_names_the_cycle(raw_registry, taxonomy) -> None:
    def change(m):
        m["operating_profit"]["inputs"]["loop"] = {"metric": "operating_margin"}
        m["operating_profit"]["formula"] = "pbt + loop"

    error = _mutate(raw_registry, taxonomy, change)
    text = str(error)
    assert "dependency cycle" in text
    assert "operating_profit" in text and "operating_margin" in text


def test_input_naming_an_unknown_concept_is_a_load_error(raw_registry, taxonomy) -> None:
    error = _mutate(
        raw_registry,
        taxonomy,
        lambda m: m["operating_profit"]["inputs"].update({"pbt": {"concept": "profit_b4_tax"}}),
    )
    assert "unknown concept 'profit_b4_tax'" in str(error)


def test_input_naming_an_unknown_metric_is_a_load_error(raw_registry, taxonomy) -> None:
    error = _mutate(
        raw_registry,
        taxonomy,
        lambda m: m["ebitda"]["inputs"].update({"op": {"metric": "no_such_metric"}}),
    )
    assert "unknown metric 'no_such_metric'" in str(error)


def test_formula_checks(raw_registry, taxonomy) -> None:
    def undeclared(m):
        m["ebitda"]["formula"] = "op + da + zzz"

    def unused(m):
        m["ebitda"]["formula"] = "op"

    def unsafe(m):
        m["ebitda"]["formula"] = "op ** da"

    assert "undeclared ['zzz']" in str(_mutate(raw_registry, taxonomy, undeclared))
    assert "not used by the formula" in str(_mutate(raw_registry, taxonomy, unused))
    assert "not allowed" in str(_mutate(raw_registry, taxonomy, unsafe))


def test_instant_and_offset_rules(raw_registry, taxonomy) -> None:
    def no_at(m):
        del m["dso"]["inputs"]["rec_c"]["at"]

    def at_on_duration(m):
        m["ebitda"]["inputs"]["da"]["at"] = "period_end"

    def duration_into_instant(m):
        m["current_ratio"]["inputs"]["ca"] = {"concept": "revenue"}

    def periods_offset_on_instant(m):
        m["advances_growth_yoy"]["inputs"]["adv_ly"]["offset"] = {"periods": -1}

    assert "needs `at`" in str(_mutate(raw_registry, taxonomy, no_at))
    assert "`at` applies to an instant" in str(_mutate(raw_registry, taxonomy, at_on_duration))
    assert "cannot feed an INSTANT" in str(_mutate(raw_registry, taxonomy, duration_into_instant))
    assert "only be offset by years" in str(
        _mutate(raw_registry, taxonomy, periods_offset_on_instant)
    )


def test_input_option_rules(raw_registry, taxonomy) -> None:
    def two_sources(m):
        m["ebitda"]["inputs"]["da"] = {"concept": "revenue", "constant": 1}

    def bad_fallback_basis(m):
        m["gross_npa"]["inputs"]["x"] = {"concept": "revenue", "fallback_basis": "standalone"}

    def mixed_kinds(m):
        m["current_ratio"]["period_types"] = ["INSTANT", "FY"]

    def metric_with_missing(m):
        m["ebitda"]["inputs"]["op"]["missing"] = "zero"

    assert "exactly one of" in str(_mutate(raw_registry, taxonomy, two_sources))
    assert "zero_is_placeholder" in str(_mutate(raw_registry, taxonomy, bad_fallback_basis))
    assert "cannot be mixed" in str(_mutate(raw_registry, taxonomy, mixed_kinds))
    assert "concepts only" in str(_mutate(raw_registry, taxonomy, metric_with_missing))


def test_a_synonym_shared_within_one_sector_is_refused(raw_registry, taxonomy) -> None:
    def clash(m):
        m["roe_bank"]["sectors"] = ["general", "bank"]

    assert "synonym 'roe' is on both" in str(_mutate(raw_registry, taxonomy, clash))


def test_registry_must_cover_period_types_of_its_inputs(raw_registry, taxonomy) -> None:
    def too_wide(m):
        m["operating_margin"]["period_types"] = ["Q", "H", "9M", "FY"]
        m["operating_profit"]["period_types"] = ["FY"]

    assert "not defined for every period type" in str(_mutate(raw_registry, taxonomy, too_wide))
