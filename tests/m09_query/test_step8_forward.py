"""Periods that are not filed yet, and forward-looking (guidance) questions."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from finrag.core.fiscal import label_to_period
from finrag.llm.fake import FakeLLM
from finrag.query.models import PERIOD_NOT_AVAILABLE, Answer, Clarification, Intent
from finrag.query.postcheck import NO_GUIDANCE

from .conftest import NETWEB, TODAY, claim, claims_json
from .test_step4_parser import make_parser
from .test_step7_service import chunks_from, hit, log_rows, make_service

pytestmark = pytest.mark.usefixtures("seeded")
D = Decimal


def P(label):
    return label_to_period(label)


def parse(q, llm=None):
    return make_parser(llm).parse(q, TODAY)


# --- a period that is not filed yet ------------------------------------------------------------


@pytest.mark.parametrize(
    "q,asked",
    [
        ("What is Netweb's revenue for FY28?", "FY28 (Apr 2027–Mar 2028)"),
        ("Gabriel operating margin Q2 FY27", "Q2 FY27 (Jul–Sep 2026)"),
        ("HDFC NII H1 FY28", "H1 FY28 (Apr–Sep 2027)"),
        ("Compare Gabriel's FY27 and FY26 EBITDA margin", "FY27 (Apr 2026–Mar 2027)"),
        ("Netweb 9M FY27 profit", "9M FY27 (Apr–Dec 2026)"),
    ],
)
def test_a_period_after_the_latest_filed_is_never_answered(q, asked) -> None:
    got = parse(q)
    assert isinstance(got, Clarification) and got.reason == PERIOD_NOT_AVAILABLE
    assert got.confidence == 0.0
    assert f"{asked} is not available yet" in got.message
    assert "the latest filed period for" in got.message and "Q1 FY27 (Apr–Jun 2026)" in got.message
    assert [c.id for c in got.candidates] == ["Q1 FY27"]


def test_the_latest_filed_period_itself_and_older_ones_still_proceed() -> None:
    assert parse("Gabriel operating margin Q1 FY27").confidence == 1.0
    old = parse("Gabriel FY19 revenue")
    assert isinstance(old, Intent) and old.periods == [P("FY19")] and old.confidence == 1.0


def test_last_quarter_fallback_is_unchanged() -> None:
    got = parse("What was Netweb's operating margin last quarter")
    assert isinstance(got, Intent) and got.decision == "confirm" and got.periods == [P("Q1 FY27")]


def test_no_data_at_all_does_not_trigger_the_future_rule() -> None:
    from finrag.query.availability import StubAvailability

    parser = make_parser(periods=[])
    parser.avail = StubAvailability([])
    got = parser.parse("Gabriel operating margin Q2 FY27", TODAY)
    assert isinstance(got, Intent) and got.periods == [P("Q2 FY27")]  # nothing to compare with


# --- forward-looking words ------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "What is Netweb's capex plan for FY28?",
        "What is HDFC's outlook on credit growth?",
        "netweb revenue guidance going forward",
        "What is Netweb's revenue target?",
        "Will Netweb grow revenue?",
        "Does Netweb management expect revenue to grow?",
        "Why will Netweb revenue rise?",  # a "why" does not beat a forward word
        "Netweb revenue forecast",
    ],
)
def test_forward_words_route_to_narrative(q) -> None:
    got = parse(q)
    assert isinstance(got, Intent) and got.forward_looking and got.question_type == "narrative"


def test_expected_credit_loss_is_not_a_forward_word() -> None:
    got = parse("What was HDFC expected credit loss in Q1 FY27")
    assert not isinstance(got, Intent) or not got.forward_looking


def test_the_capex_plan_question_found_with_explain_plan() -> None:
    got = parse("What is Netweb's capex plan for FY28?")
    assert isinstance(got, Intent) and got.confidence < 1.0 and got.decision == "proceed"
    assert got.metrics == ["capex_ppe"] and got.question_type == "narrative"
    assert got.horizon == [P("FY28")] and got.periods == [P("Q1 FY27")]
    assert "period" in got.defaults_applied
    assert got.search_quarters == [P("Q4 FY26"), P("Q1 FY27")]
    assert "management guidance and outlook on Purchase of property, plant and equipment" in (
        got.interpretation
    )
    assert "for FY28 (Apr 2027–Mar 2028), which is not filed yet" in got.interpretation
    assert "last reported actuals shown for reference: Q1 FY27" in got.interpretation


def test_a_filed_period_named_in_a_guidance_question_is_kept_as_evidence() -> None:
    got = parse("What was the guidance in Q4 FY26 for Netweb revenue?")
    assert got.periods == [P("Q4 FY26")] and got.horizon == [] and got.search_quarters == []


def test_the_plan_searches_the_latest_calls_not_the_horizon() -> None:
    service = make_service()
    intent, plan = service.explain_plan("What is Netweb's capex plan for FY28?", TODAY)
    groups = [(g["source_type"], g["periods"]) for g in plan.search_groups]
    assert groups == [
        ("transcript", ["Q4 FY26", "Q1 FY27"]),
        ("results", ["Q1 FY27"]),
    ]
    assert "FY28" not in json.dumps(plan.to_json()["searches"])
    assert [r.period for r in plan.requests if r.role == "main"] == [P("Q1 FY27")]


# --- the answer ---------------------------------------------------------------------------


@pytest.fixture
def netweb_actuals(kit):
    kit.fact(NETWEB, "capex_ppe", "Q1 FY27", 119790000, basis="standalone")
    return kit


ASK = "What is Netweb's capex plan for FY28?"


def test_the_numbers_are_labelled_actuals_not_guidance(netweb_actuals, monkeypatch) -> None:
    chunks_from(monkeypatch, {})
    answer = make_service(FakeLLM(default="never")).ask(ASK, TODAY)
    assert isinstance(answer, Answer)
    assert (
        "These are reported actuals for Q1 FY27 (Apr–Jun 2026); they are not management "
        "guidance for FY28 (Apr 2027–Mar 2028)."
    ) in answer.numbers_block
    assert "Q1 FY27 (Apr–Jun 2026) (actual), standalone: 11.98 crore" in answer.numbers_block


def test_nothing_retrieved_says_guidance_is_not_in_the_filings(netweb_actuals, monkeypatch) -> None:
    chunks_from(monkeypatch, {})
    llm = FakeLLM(default="never")
    answer = make_service(llm).ask(ASK, TODAY)
    assert answer.explanation == NO_GUIDANCE == "Management guidance is not in the indexed filings."
    assert llm.calls == [] and "**What management said**" in answer.text


CAPEX_QUOTE = "We plan capex of 200 crore next year."


def test_guidance_passages_are_summarised_with_the_guidance_prompt(
    netweb_actuals, monkeypatch
) -> None:
    chunks_from(monkeypatch, {"transcript": [hit(CAPEX_QUOTE, "CHK-1", label="Q1 FY27")]})
    llm = FakeLLM(
        default=claims_json(
            claim("Management said it plans capex of 200 crore next year.", ["N1"], CAPEX_QUOTE)
        )
    )
    answer = make_service(llm).ask(ASK, TODAY)
    assert answer.explanation == "Management said it plans capex of 200 crore next year [N1]."
    assert f"  > “{CAPEX_QUOTE}”" in answer.text
    prompt = llm.calls[0][1]
    assert "Figures are the last reported actuals. They are not guidance." in prompt
    assert "Never present an actual figure as guidance" in prompt


def test_a_guidance_claim_with_an_invented_number_is_dropped(netweb_actuals, monkeypatch) -> None:
    chunks_from(
        monkeypatch,
        {"transcript": [hit("Capex will rise next year in line with demand.", "CHK-1")]},
    )
    quote = "Capex will rise next year in line with demand."
    llm = FakeLLM(default=claims_json(claim("Management plans capex of 500 crore.", ["N1"], quote)))
    answer = make_service(llm).ask(ASK, TODAY)
    assert answer.explanation == NO_GUIDANCE and answer.dropped


def test_an_analysts_question_is_not_guidance_and_a_figure_is_not_management(
    netweb_actuals, monkeypatch
) -> None:
    quote = "Can you tell us the capex plan for FY28 and how it will be funded?"
    chunks_from(
        monkeypatch,
        {"transcript": [hit(quote, "CHK-1", speaker="Ravi", role="analyst", firm="HSBC")]},
    )
    llm = FakeLLM(
        default=claims_json(
            claim("Management said it will fund the FY28 capex plan.", ["N1"], quote),
            claim("Management plans capex of 11.98 crore.", ["N1", "F1"], quote),
        )
    )
    answer = make_service(llm).ask(ASK, TODAY)
    assert answer.explanation == NO_GUIDANCE
    assert [d["reason"] for d in answer.dropped] == [
        "management_not_speaking",
        "figure_attributed_to_management",
    ]


def test_the_model_may_say_there_is_no_guidance(netweb_actuals, monkeypatch) -> None:
    chunks_from(monkeypatch, {"transcript": [hit("Revenue was good.", "CHK-1")]})
    answer = make_service(FakeLLM(default=claims_json())).ask(ASK, TODAY)
    assert answer.explanation == NO_GUIDANCE and not answer.dropped


def test_the_guidance_question_searches_management_turns_only(netweb_actuals) -> None:
    _, plan = make_service().explain_plan(ASK, TODAY)
    assert plan.search_groups[0]["speaker_roles"] == ["management"]
    assert plan.searches[0].speaker_roles == ["management"]
    assert plan.searches[1].speaker_roles is None  # the results text has no speakers


def test_a_refused_future_period_is_logged_and_writes_no_answer(kit) -> None:
    out = make_service(FakeLLM(default="never")).ask("What is Netweb's revenue for FY28?", TODAY)
    assert isinstance(out, Clarification)
    (row,) = log_rows()
    assert json.loads(row.intent_json)["reason"] == PERIOD_NOT_AVAILABLE
