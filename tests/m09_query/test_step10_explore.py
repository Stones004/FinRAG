"""Exploratory search for a question with no metric and no cue, no Figures block when no metric is
asked, hedges kept, and the honest note when no passage was usable."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from finrag.core.config import get_settings
from finrag.llm.fake import FakeLLM
from finrag.query.answerer import NO_TRANSCRIPT_NOTE, NO_USABLE_PASSAGE_NOTE
from finrag.query.claims import Claim, check_claim
from finrag.query.intents import load_intents
from finrag.query.models import NO_NARRATIVE_MATCH, Answer, Clarification, Intent

from .conftest import GABRIEL, TODAY, claim, claims_json
from .test_step4_parser import make_parser
from .test_step7_service import chunks_from, hit, log_rows, make_service
from .test_step9_evidence import call, rules

pytestmark = pytest.mark.usefixtures("seeded")
D = Decimal

SCHEME_Q = "What scheme of arrangement affected Gabriel's Q1 FY27 results?"
SCHEME = (
    "The Board approved a Composite Scheme of Arrangement involving the merger of Anchemco India "
    "Private Limited, and the Scheme has been approved by the National Company Law Tribunal."
)


def scored(h, dense):
    return replace(h, scores={"rrf": 0.03, "bm25": 7.0, "dense": dense})


# --- no metric, no cue ------------------------------------------------------------------------


def test_distinctive_words_make_the_question_exploratory() -> None:
    got = make_parser().parse(SCHEME_Q, TODAY)
    assert isinstance(got, Intent) and got.exploratory and got.question_type == "narrative"
    assert got.metrics == [] and "search of the filed results text" in got.interpretation
    assert "basis" not in got.interpretation  # no figure is shown, so no basis to state


@pytest.mark.parametrize(
    "q", ["how is HDFC doing?", "tell me about Netweb", "gabriel margin", "Gabriel Sharpe ratio"]
)
def test_a_question_without_distinctive_words_is_still_asked_about(q) -> None:
    assert isinstance(make_parser().parse(q, TODAY), Clarification)


def test_the_exploratory_plan_searches_every_results_section_and_no_call() -> None:
    service = make_service()
    intent, plan = service.explain_plan(SCHEME_Q, TODAY)
    assert [(r.source_types, r.sections) for r in plan.searches] == [(["results"], None)]
    assert plan.requests == [] and plan.search_groups[0]["sections"] is None


def test_a_close_hit_is_answered_without_a_figures_block(monkeypatch) -> None:
    close = scored(hit(SCHEME, "CHK-1", source="results", section="auditor_report"), 0.7)
    chunks_from(monkeypatch, {"results": [close]})
    quote = "approved a Composite Scheme of Arrangement involving the merger of Anchemco"
    llm = FakeLLM(
        default=claims_json(
            claim("The Board approved a Composite Scheme of Arrangement.", ["N1"], quote)
        )
    )
    answer = make_service(llm).ask(SCHEME_Q, TODAY)
    assert isinstance(answer, Answer) and "Composite Scheme of Arrangement" in answer.text
    assert "**Figures**" not in answer.text and answer.numbers_block == ""
    assert answer.text.splitlines()[0].startswith("Reading your question as:")


def test_a_hit_below_the_threshold_is_a_logged_clarification_and_no_llm_call(monkeypatch) -> None:
    far = scored(hit(SCHEME, "CHK-1", source="results"), 0.5)
    assert 0.5 < get_settings().query.retrieval.explore_min_dense_score
    chunks_from(monkeypatch, {"results": [far]})
    llm = FakeLLM(default="never")
    got = make_service(llm).ask(SCHEME_Q, TODAY)
    assert isinstance(got, Clarification) and got.reason == NO_NARRATIVE_MATCH
    assert llm.calls == [] and got.query_id
    assert log_rows()[-1].query_id == got.query_id


def test_no_hit_at_all_is_the_same_clarification(monkeypatch) -> None:
    chunks_from(monkeypatch, {"results": []})
    got = make_service(FakeLLM(default="never")).ask(SCHEME_Q, TODAY)
    assert isinstance(got, Clarification) and got.reason == NO_NARRATIVE_MATCH


# --- no metric asked -> no Figures block ------------------------------------------------------


def test_a_management_question_with_no_metric_has_no_figures_block(monkeypatch) -> None:
    text = "Our order book stood at INR25,069.35 million as of 30th June 2026"
    turn = hit(text, "CHK-T1", source="transcript", section="prepared_remarks")
    chunks_from(monkeypatch, {"transcript": [turn], "results": []})
    llm = FakeLLM(default=claims_json(claim("The order book is large.", ["N1"], text)))
    answer = make_service(llm).ask("What did Gabriel management say about the order book?", TODAY)
    assert isinstance(answer, Answer)
    assert "**Figures**" not in answer.text and "No data" not in answer.text
    assert answer.numbers_block == ""


# --- hedges ----------------------------------------------------------------------------------

HEDGED = "maybe around 40% to 45% should be around on the AI side of it and balance would be other"


def drop_reason(text, quote, role="management"):
    rule = replace(rules(), hedge_words=tuple(load_intents().hedge_words))
    passages = {"N1": call(quote, role=role)}
    return check_claim(Claim(text, ("N1",), quote), passages, {}, rule)


def test_a_hedged_quote_cannot_become_a_flat_claim() -> None:
    assert drop_reason("The order book mix is 40% to 45% on the AI side.", HEDGED) == (
        "hedge_not_kept"
    )


def test_a_claim_that_keeps_a_hedge_passes() -> None:
    text = "Management said the order book mix is around 40% to 45% on the AI side."
    assert drop_reason(text, HEDGED) is None
    assert drop_reason("Management expects 40% to 45% on the AI side.", HEDGED) is None


def test_an_unhedged_quote_needs_no_hedge() -> None:
    quote = "Our order book stood at INR25,069.35 million as of 30th June"
    text = "Management said the order book stood at INR25,069.35 million."
    assert drop_reason(text, quote) is None


def test_the_month_of_may_is_not_a_hedge() -> None:
    quote = "The scheme was approved by the tribunal by its order dated May 11, 2026"
    assert drop_reason("The tribunal approved the scheme.", quote, role="results") is None
    assert drop_reason("Management said it was approved.", "It may well be approved") == (
        "hedge_not_kept"
    )


# --- the note when no passage was usable -------------------------------------------------------


def test_the_note_says_no_usable_passage_when_every_claim_was_dropped(kit, monkeypatch) -> None:
    kit.metric(GABRIEL, "profit_after_tax", "Q1 FY27", D("1081260000"))
    results = hit(SCHEME, "CHK-1", source="results", section="results_notes")
    chunks_from(monkeypatch, {"transcript": [], "results": [results]})
    llm = FakeLLM(default=claims_json(claim("Profit rose due to the Scheme.", ["N1"], "not in it")))
    answer = make_service(llm).ask("Why did Gabriel's PAT jump in Q1 FY27?", TODAY)
    assert NO_USABLE_PASSAGE_NOTE in answer.warnings and NO_TRANSCRIPT_NOTE not in answer.warnings
    assert "the explanation uses the results text" not in answer.text


def test_the_old_note_stays_when_a_claim_was_kept(kit, monkeypatch) -> None:
    kit.metric(GABRIEL, "profit_after_tax", "Q1 FY27", D("1081260000"))
    results = hit(SCHEME, "CHK-1", source="results", section="results_notes")
    chunks_from(monkeypatch, {"transcript": [], "results": [results]})
    quote = "approved a Composite Scheme of Arrangement involving the merger"
    llm = FakeLLM(
        default=claims_json(claim("The Board approved a Composite Scheme.", ["N1"], quote))
    )
    answer = make_service(llm).ask("Why did Gabriel's PAT jump in Q1 FY27?", TODAY)
    assert NO_TRANSCRIPT_NOTE in answer.warnings and NO_USABLE_PASSAGE_NOTE not in answer.warnings
