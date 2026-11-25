"""Follow-ups of the UI build: growth companions on change words; event-covered warnings."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from finrag.core.config import get_settings, load_registry
from finrag.metrics.registry import MetricsError, load_metrics, parse_registry
from finrag.normalize.taxonomy import load_taxonomy
from finrag.query.intents import load_intents
from finrag.query.numbers_block import NumbersBlockBuilder

from .conftest import GABRIEL
from .test_step5_plan_retrieve import intent, planner  # noqa: F401
from .test_step6_answer import Env, make_intent

pytestmark = pytest.mark.usefixtures("seeded")


def companions(plan) -> list[str]:
    return [r.metric for r in plan.requests if r.role == "companion"]


def ask(planner, question, metrics, qtype="lookup", periods=("Q1 FY27",)):  # noqa: F811
    base = intent(metrics=metrics, periods=periods, qtype=qtype)
    return planner.plan(replace(base, question=question))


# --- (b) change words add the growth sibling ---------------------------------------------------


def test_a_change_word_adds_the_growth_metric_of_a_concept(planner) -> None:  # noqa: F811
    plan = ask(planner, "why did profit jump in Q1 FY27", ["profit_after_tax"], "explanation")
    assert "pat_growth_yoy" in companions(plan)


def test_a_change_word_adds_the_growth_companion_of_a_metric(planner) -> None:  # noqa: F811
    plan = ask(planner, "operating margin fell in Q1 FY27", ["operating_margin"])
    assert companions(plan) == ["operating_margin_change_yoy"]


@pytest.mark.parametrize("word", ["go up", "went down", "surged", "rose", "dropped", "declined"])
def test_each_change_word_triggers_it(planner, word) -> None:  # noqa: F811
    plan = ask(planner, f"did revenue {word}", ["revenue"])
    assert companions(plan) == ["revenue_growth_yoy"]


def test_no_change_word_adds_nothing(planner) -> None:  # noqa: F811
    assert companions(ask(planner, "what is operating margin", ["operating_margin"])) == []


def test_a_metric_without_a_growth_sibling_gets_nothing(planner) -> None:  # noqa: F811
    assert companions(ask(planner, "net margin fell", ["net_margin"])) == []


def test_a_growth_metric_asked_for_is_not_repeated(planner) -> None:  # noqa: F811
    plan = ask(planner, "profit growth rose", ["pat_growth_yoy"])
    assert "pat_growth_yoy" not in companions(plan)


def test_the_sibling_follows_the_period_asked(planner) -> None:  # noqa: F811
    plan = ask(planner, "revenue rose", ["revenue"], periods=("FY26",))
    assert [(r.metric, r.period.type.value) for r in plan.requests if r.role == "companion"] == [
        ("revenue_growth_yoy", "FY")
    ]


def test_the_change_words_live_in_the_registry() -> None:
    words = load_intents().change_words
    assert {"jump", "surge", "rise", "rose", "increase", "fall", "drop", "decline", "grew"} <= set(
        words
    )
    assert {"growth", "go up", "go down"} <= set(words)


def test_a_growth_companion_must_name_another_metric() -> None:
    raw = load_registry("metrics")
    raw["metrics"]["operating_margin"]["growth_companion"] = "no_such_metric"
    with pytest.raises(MetricsError, match="growth_companion 'no_such_metric'"):
        parse_registry(raw, load_taxonomy())
    raw["metrics"]["operating_margin"]["growth_companion"] = "operating_margin"
    with pytest.raises(MetricsError, match="growth_companion"):
        parse_registry(raw, load_taxonomy())


def test_every_growth_companion_in_the_registry_is_a_metric() -> None:
    registry = load_metrics(load_taxonomy())
    named = [registry.get(m).growth_companion for m in registry.order()]
    assert "operating_margin_change_yoy" in named
    assert all(n is None or n in registry for n in named)


# --- (a) the xbrl_vs_pdf warning is not repeated when an event covers the comparative -------------


def _warning_text(kit, rules, warn_end="2025-06-30", event_date="2026-04-01", rule="xbrl_vs_pdf"):
    env = Env()
    env.builder = NumbersBlockBuilder(
        env.fmt, env.registry, env.taxonomy, env.settings.defaults.display_unit, rules
    )
    kit.metric(
        GABRIEL,
        "pat_growth_yoy",
        "Q1 FY27",
        Decimal("0.3646"),
        details={
            "code": "computed",
            "basis_used": "consolidated",
            "basis_change": [
                {
                    "event_id": 2,
                    "event_type": "comparative_restatement",
                    "event_date": event_date,
                    "citation": "Scheme.",
                }
            ],
            "warnings": [{"concept": "profit_after_tax", "period_end": warn_end, "rule_id": rule}],
        },
    )
    return env.run(make_intent(metrics=["pat_growth_yoy"], periods=["Q1 FY27"]))[2].text


def test_the_warning_is_dropped_when_a_recorded_event_covers_the_period(kit) -> None:
    text = _warning_text(kit, ("xbrl_vs_pdf",))
    assert "Basis change" in text and "Validation warning" not in text


def test_the_warning_stays_without_the_setting(kit) -> None:
    assert "Validation warning (xbrl_vs_pdf)" in _warning_text(kit, ())


def test_the_warning_stays_for_a_period_after_the_event(kit) -> None:
    text = _warning_text(kit, ("xbrl_vs_pdf",), warn_end="2026-06-30")
    assert "Validation warning (xbrl_vs_pdf)" in text


def test_other_rules_are_not_dropped(kit) -> None:
    text = _warning_text(kit, ("xbrl_vs_pdf",), rule="sum_check")
    assert "Validation warning (sum_check)" in text


def test_the_service_gets_the_rules_from_settings() -> None:
    from finrag.query.service import build_service

    assert get_settings().query.answer.event_covered_rules == ["xbrl_vs_pdf"]
    assert build_service().builder.event_covered_rules == {"xbrl_vs_pdf"}
