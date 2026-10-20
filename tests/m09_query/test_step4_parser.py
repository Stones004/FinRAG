"""Step 4: intent parser, LLM fallback, and the routing set (tuning and held-out reported apart)."""

from __future__ import annotations

from pathlib import Path

import pytest

from finrag.core.config import get_settings
from finrag.core.fiscal import label_to_period
from finrag.llm.fake import FakeLLM
from finrag.metrics.registry import load_metrics
from finrag.normalize.taxonomy import load_taxonomy
from finrag.query.availability import StubAvailability
from finrag.query.entity_resolver import load_entities
from finrag.query.intent_parser import IntentParser
from finrag.query.models import (
    AMBIGUOUS_METRIC,
    CONFIRM,
    LOW_CONFIDENCE,
    MISSING_METRIC,
    PROCEED,
    UNKNOWN_METRIC,
    Clarification,
    Intent,
)
from finrag.query.routing import load_cases, run_routing

from .conftest import GABRIEL, HDFC, NETWEB, TODAY

pytestmark = pytest.mark.usefixtures("seeded")
CASES = Path(__file__).parent / "routing_cases.yaml"
ISINS = {"GABRIEL": GABRIEL, "HDFCBANK": HDFC, "NETWEB": NETWEB}


def make_parser(llm=None, periods=None) -> IntentParser:
    labels = ["Q1 FY26", "Q2 FY26", "Q3 FY26", "Q4 FY26", "Q1 FY27", "H1 FY26", "FY25", "FY26"]
    taxonomy = load_taxonomy()
    from finrag.query.intents import load_intents

    return IntentParser(
        entities=load_entities(),
        registry=load_metrics(taxonomy),
        taxonomy=taxonomy,
        intents=load_intents(taxonomy),
        settings=get_settings(),
        availability=StubAvailability(periods or [label_to_period(x) for x in labels]),
        llm=llm,
    )


def parse(q: str, llm=None) -> Intent | Clarification:
    return make_parser(llm).parse(q, TODAY)


# --- decisions -------------------------------------------------------------------------------


def test_clean_question_proceeds_and_reads_back_the_interpretation() -> None:
    got = parse("What was Gabriel's operating margin in Q1 FY27?")
    assert isinstance(got, Intent) and got.decision == PROCEED and got.confidence == 1.0
    assert got.interpretation == (
        "Reading your question as: Gabriel India Limited — Operating margin — "
        "Q1 FY27 (Apr–Jun 2026) — consolidated basis (company default)."
    )


def test_last_quarter_names_both_periods_and_asks() -> None:
    got = parse("What was Netweb's operating margin last quarter")
    assert got.decision == CONFIRM and 0.6 <= got.confidence < 0.85
    assert "Q2 FY27 (Jul–Sep 2026) is not filed" in got.interpretation
    assert "showing Q1 FY27 (Apr–Jun 2026)" in got.interpretation
    assert got.interpretation.endswith("Is this right?")


def test_fuzzy_match_below_threshold_asks() -> None:
    got = parse("netwab revnue growth fy26")
    assert got.decision == CONFIRM and got.entity_isin == NETWEB


def test_basis_precedence() -> None:
    cti = parse("HDFC cost to income FY26")
    assert cti.basis == "standalone" and "basis:metric_preferred" in cti.defaults_applied
    assert "standalone basis (preferred for this metric)" in cti.interpretation
    named = parse("HDFC cost to income consolidated FY26")
    assert named.basis == "consolidated" and "basis:metric_preferred" not in named.defaults_applied
    assert parse("Netweb ebitda margin FY26").basis == "standalone"  # entity default
    assert parse("Gabriel ebitda margin FY26").basis == "consolidated"  # company default
    assert parse("HDFC revenue FY26").basis == "consolidated"  # nothing preferred


def test_consolidated_vs_standalone_is_a_basis_comparison_not_a_clarification() -> None:
    for q in (
        "Gabriel consolidated vs standalone operating margin FY26",
        "Gabriel standalone vs consolidated operating margin FY26",
        "Gabriel operating margin FY26 consolidated and standalone",
    ):
        got = parse(q)
        assert isinstance(got, Intent) and got.comparison == "basis" and got.basis is None
        assert got.question_type == "comparison"
        assert "consolidated and standalone" in got.interpretation


def test_defaulted_period_is_stated() -> None:
    got = parse("HDFC GNPA latest")
    assert got.decision == PROCEED and "period" in got.defaults_applied
    assert "No period was named, so the latest with data is shown." in got.interpretation


def test_narrative_needs_no_metric() -> None:
    got = parse("Netweb guidance for FY27")
    assert got.question_type == "narrative" and got.metrics == []
    got = parse("What are Gabriel's contingent liabilities?")
    assert got.unavailable_sources == ["contingent"] and got.question_type == "narrative"


@pytest.mark.parametrize(
    "q,reason",
    [
        ("what's the operating margin?", "no_entity"),
        ("Reliance revenue FY26", "unknown_company"),
        ("compare Gabriel and Netweb ROE", "multi_entity"),
        ("gabriel margin", AMBIGUOUS_METRIC),
        ("How are Netweb's margins?", AMBIGUOUS_METRIC),
        ("Gabriel Sharpe ratio", UNKNOWN_METRIC),
        ("how is HDFC doing?", MISSING_METRIC),
    ],
)
def test_clarification_reasons(q, reason) -> None:
    got = parse(q)
    assert isinstance(got, Clarification) and got.reason == reason and got.message


def test_unknown_metric_lists_nearest_registry_metrics() -> None:
    got = parse("Gabriel Sharpe ratio")
    assert got.candidates and all(c.id for c in got.candidates)
    assert "I have no metric called 'sharpe'" in got.message


def test_below_clarify_threshold_is_a_low_confidence_clarification() -> None:
    parser = make_parser()
    parser.query = parser.query.model_copy(
        update={"intent": parser.query.intent.model_copy(update={"unfiled_period_confidence": 0.4})}
    )
    got = parser.parse("Gabriel ROCE last quarter", TODAY)
    assert isinstance(got, Clarification) and got.reason == LOW_CONFIDENCE


# --- LLM fallback ----------------------------------------------------------------------------


def llm_reply(entity="none", metric="none", confidence="high") -> FakeLLM:
    import json

    return FakeLLM(
        default=json.dumps(
            {"entity": entity, "metric": metric, "confidence": confidence, "reason": "test"}
        )
    )


def test_llm_not_called_when_rules_are_confident() -> None:
    llm = llm_reply()
    parse("What was Gabriel's operating margin in Q1 FY27?", llm)
    assert llm.calls == []


def test_llm_picks_from_the_shortlist_when_the_rules_find_the_metric_ambiguous() -> None:
    llm = llm_reply(metric="fcf", confidence="medium")
    got = make_parser(llm).parse("Gabriel how much cash did the business make in FY26", TODAY)
    assert isinstance(got, Intent) and got.metrics == ["fcf"] and got.llm_used
    assert got.confidence == 0.7 and got.decision == CONFIRM
    sent = llm.calls[0][1]
    assert "fcf: Free cash flow" in sent and "operating_margin" not in sent
    assert llm.calls[0][2]["required"] == ["entity", "metric", "confidence", "reason"]


def test_llm_choice_outside_the_shortlist_is_discarded_and_user_is_asked() -> None:
    llm = llm_reply(metric="not_a_real_metric", entity="INE000000000")
    got = make_parser(llm).parse("Gabriel Sharpe ratio", TODAY)
    assert isinstance(got, Clarification) and got.reason == UNKNOWN_METRIC
    assert len(llm.calls) == 1


def test_llm_down_falls_back_to_the_rules() -> None:
    down = FakeLLM(default="x", available=False)
    got = make_parser(down).parse("Gabriel Sharpe ratio", TODAY)
    assert isinstance(got, Clarification) and got.reason == UNKNOWN_METRIC


def test_llm_agreeing_with_a_weak_fuzzy_entity_raises_its_confidence() -> None:
    llm = llm_reply(entity=NETWEB, metric="ebitda", confidence="high")
    got = make_parser(llm).parse("netwab revnue growth fy26", TODAY)
    assert isinstance(got, Intent) and got.entity_isin == NETWEB and got.llm_used
    assert got.field_confidence["entity"] == 0.9 and got.decision == PROCEED
    # the metric was strong (fuzzy 0.96, not weak): no metric shortlist, so "ebitda" is discarded
    assert got.metrics == ["revenue_growth_yoy"]
    assert "ebitda" not in llm.calls[0][1]


def test_llm_never_downgrades_a_strong_entity() -> None:
    llm = llm_reply(entity=GABRIEL, metric="fcf", confidence="low")
    got = make_parser(llm).parse("Gabriel how much cash did the business make in FY26", TODAY)
    # a "low" metric choice (0.5) is below the clarify threshold: the user is asked
    assert isinstance(got, Clarification) and got.reason == LOW_CONFIDENCE
    assert got.partial["field_confidence"] == {"entity": 1.0, "period": 1.0, "metric": 0.5}


# --- the routing set -------------------------------------------------------------------------


def test_routing_set_has_the_agreed_shape() -> None:
    cases = load_cases(CASES)["cases"]
    held = [c for c in cases if c.get("held_out")]
    assert len(cases) == 81 and len(held) == 28
    assert len({c["id"] for c in cases}) == 81
    assert sum("clarify" in c for c in cases) == 15


def test_routing_accuracy_and_no_wrong_confident(capsys) -> None:
    result = run_routing(make_parser(), ISINS, CASES)
    print("\n" + result.summary())
    for r in result.failures():
        print(f"  {r.id} {r.question!r}: {'; '.join(r.problems)}")
    out = capsys.readouterr().out
    assert not result.wrong_confident, out
    target = get_settings().query.routing.min_accuracy
    assert result.rate(result.group(False)) >= target, out
    assert result.rate(result.group(True)) >= target, out
