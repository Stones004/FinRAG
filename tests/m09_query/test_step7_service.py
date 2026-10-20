"""Step 7: the service end to end on a temp database (M09 acceptance 2, 3, 6-10, 12, 13)."""

from __future__ import annotations

import json
from decimal import Decimal

import numpy as np
import pytest
import sqlalchemy as sa

from finrag.core.config import get_settings
from finrag.core.db import tables, transaction
from finrag.core.fiscal import label_to_period
from finrag.llm.fake import FakeLLM
from finrag.metrics.registry import load_metrics
from finrag.narrative.search import SearchHit
from finrag.normalize.taxonomy import load_taxonomy
from finrag.query.answerer import Answerer
from finrag.query.availability import StubAvailability
from finrag.query.entity_resolver import load_entities
from finrag.query.formatter import Formatter
from finrag.query.intent_parser import IntentParser
from finrag.query.intents import load_intents
from finrag.query.models import Answer, Clarification
from finrag.query.numbers_block import NumbersBlockBuilder
from finrag.query.planner import Planner
from finrag.query.postcheck import NO_CHUNKS, NO_EXPLANATION
from finrag.query.retriever import Retriever
from finrag.query.service import LLM_DOWN_NOTE, QueryService

from .conftest import GABRIEL, HDFC, TODAY, claim, claims_json

pytestmark = pytest.mark.usefixtures("seeded")
D = Decimal


class Plain:
    """An embedder for which every text is equally relevant (MMR keeps the search order)."""

    name = "plain"

    def embed(self, texts):
        return np.eye(max(len(texts), 1), 64, dtype=np.float32)[: len(texts)]

    def embed_query(self, text):
        return np.ones(64, dtype=np.float32) / 8


def make_service(llm=None) -> QueryService:
    settings = get_settings()
    taxonomy = load_taxonomy()
    registry = load_metrics(taxonomy)
    intents = load_intents(taxonomy)
    labels = ["Q1 FY26", "Q4 FY26", "Q1 FY27", "FY25", "FY26"]
    avail = StubAvailability([label_to_period(x) for x in labels])
    parser = IntentParser(load_entities(), registry, taxonomy, intents, settings, avail, llm)
    return QueryService(
        parser=parser,
        planner=Planner(registry, taxonomy, intents, settings),
        retriever=Retriever(Plain(), settings, registry, taxonomy),
        answerer=Answerer(llm, intents),
        builder=NumbersBlockBuilder(
            Formatter(settings, registry, taxonomy),
            registry,
            taxonomy,
            settings.defaults.display_unit,
        ),
        settings=settings,
        llm=llm,
    )


def hit(
    text,
    cid,
    source="transcript",
    label="Q1 FY27",
    section="qa",
    page=5,
    speaker="Anand",
    role="management",
    firm=None,
) -> SearchHit:
    return SearchHit(
        chunk_id=cid,
        filing_id="FIL-aaa",
        section=section,
        source_type=source,
        speaker=speaker,
        speaker_role=role,
        firm=firm,
        page=page,
        end_page=page,
        concept=None,
        basis="consolidated",
        fiscal_year="FY27",
        period_start="2026-04-01",
        period_end="2026-06-30",
        period_type="Q",
        period_label=label,
        text=text,
    )


def chunks_from(monkeypatch, by_source: dict[str, list[SearchHit]]) -> None:
    def _search(request, embedder, settings=None, engine=None):
        return list(by_source.get(request.source_types[0], []))

    monkeypatch.setattr("finrag.query.retriever.search", _search)


def log_rows():
    with transaction() as conn:
        return list(conn.execute(sa.select(tables.query_log).order_by(tables.query_log.c.query_id)))


@pytest.fixture
def data(kit):
    kit.metric(GABRIEL, "operating_margin", "Q1 FY27", D("0.14489"))
    kit.metric(GABRIEL, "operating_margin", "Q1 FY26", D("0.13291"))
    return kit


ASK = "Why did Gabriel's operating margin fall in Q1 FY27?"


# --- lookups ---------------------------------------------------------------------------------


def test_lookup_answer_has_interpretation_numbers_sources_and_no_llm_call(data) -> None:
    llm = FakeLLM(default="never")
    service = make_service(llm)
    answer = service.ask("What was Gabriel's operating margin in Q1 FY27?", TODAY)
    assert isinstance(answer, Answer) and llm.calls == []
    assert answer.interpretation.startswith("Reading your question as: Gabriel India Limited")
    lines = answer.text.splitlines()
    assert lines[0] == answer.interpretation
    assert "consolidated basis (company default)" in answer.numbers_block  # acceptance 10
    assert "[F1] Operating margin, Q1 FY27 (Apr–Jun 2026), consolidated: 14.49%" in answer.text
    assert "**Sources**" in answer.text and answer.citations[0].alias == "F1"
    assert answer.explanation == ""


def test_acceptance_9_every_ask_writes_one_log_row_with_plan_and_citations(data) -> None:
    service = make_service(FakeLLM(default="x"))
    answer = service.ask("What was Gabriel's operating margin in Q1 FY27?", TODAY)
    (row,) = log_rows()
    assert row.query_id == answer.query_id and row.question.startswith("What was Gabriel")
    assert json.loads(row.intent_json)["metrics"] == ["operating_margin"]
    plan = json.loads(row.plan_json)
    assert plan["requests"][0]["metric"] == "operating_margin" and "searches" in plan
    assert json.loads(row.cited_ids_json) == [answer.citations[0].id]
    assert json.loads(row.retrieved_ids_json) == [answer.citations[0].id]
    assert row.answer == answer.text and row.user_feedback is None
    service.ask("what's the margin?", TODAY)  # a clarification is logged too
    assert len(log_rows()) == 2


def test_query_log_can_be_switched_off(data, isolated_repo) -> None:
    import yaml

    from finrag.core.config import reset_settings_cache

    path = isolated_repo / "config" / "settings.yaml"
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    d["logging"]["query_log_enabled"] = False
    path.write_text(yaml.safe_dump(d), encoding="utf-8")
    reset_settings_cache()
    answer = make_service().ask("What was Gabriel's operating margin in Q1 FY27?", TODAY)
    assert answer.query_id is None and log_rows() == []


def test_feedback_is_stored_on_the_row(data) -> None:
    service = make_service()
    answer = service.ask("What was Gabriel's operating margin in Q1 FY27?", TODAY)
    service.feedback(answer.query_id, False, "wrong period")
    fb = json.loads(log_rows()[0].user_feedback)
    assert fb["correct"] is False and fb["note"] == "wrong period"
    with pytest.raises(KeyError):
        service.feedback("QRY-nope", True)


def test_explain_plan_calls_no_llm_and_writes_no_log(data) -> None:
    llm = FakeLLM(default="x")
    intent, plan = make_service(llm).explain_plan(ASK, TODAY)
    assert (
        intent.question_type == "explanation"
        and plan.search_groups[0]["source_type"] == "transcript"
    )
    assert llm.calls == [] and log_rows() == []


# --- clarifications: acceptance 2, 3 ---------------------------------------------------------


def test_unknown_metric_is_a_clarification_with_nearest_metrics_and_no_answer(data) -> None:
    llm = FakeLLM(
        default=json.dumps(
            {"entity": "none", "metric": "none", "confidence": "low", "reason": "no match"}
        )
    )
    out = make_service(llm).ask("Gabriel Sharpe ratio", TODAY)
    assert isinstance(out, Clarification) and out.reason == "unknown_metric" and out.candidates
    assert all(
        call[2] is not None for call in llm.calls
    )  # only the classifier ran, never an answer


def test_a_classifier_id_outside_the_shortlist_is_discarded_and_the_user_is_asked(data) -> None:
    llm = FakeLLM(
        default=json.dumps(
            {"entity": "none", "metric": "sharpe_ratio", "confidence": "high", "reason": "x"}
        )
    )
    out = make_service(llm).ask("Gabriel Sharpe ratio", TODAY)
    assert isinstance(out, Clarification) and out.reason == "unknown_metric"


# --- explanations: acceptance 4, 5, 6, 8, 12, 13 ---------------------------------------------


COST_QUOTE = "Raw material costs rose by 150 basis points in the quarter."
NOTE_QUOTE = "Note 5 changed the valuation of inventory to a moving weighted average."


def test_explanation_passes_the_check_shows_the_quote_and_cites_the_passage(
    data, monkeypatch
) -> None:
    chunks_from(
        monkeypatch,
        {
            "transcript": [hit(COST_QUOTE, "CHK-1")],
            "results": [
                hit(
                    NOTE_QUOTE,
                    "CHK-2",
                    source="results",
                    section="results_notes",
                    speaker=None,
                    role=None,
                    page=9,
                )
            ],
        },
    )
    llm = FakeLLM(
        default=claims_json(
            claim("Management said costs rose by 150 basis points.", ["N1"], COST_QUOTE),
            claim("The margin was 14.49%.", ["F1"], COST_QUOTE),  # a figure alone is no claim
        )
    )
    answer = make_service(llm).ask(ASK, TODAY)
    assert answer.explanation == "Management said costs rose by 150 basis points [N1]."
    assert answer.claims == [
        {
            "text": "Management said costs rose by 150 basis points.",
            "cites": ["N1"],
            "quote": COST_QUOTE,
        }
    ]
    assert {c.alias for c in answer.citations} >= {"F1", "N1"}
    n1 = next(c for c in answer.citations if c.alias == "N1")
    assert (n1.detail["source_type"], n1.detail["period_label"]) == ("transcript", "Q1 FY27")
    assert (n1.detail["speaker"], n1.detail["speaker_role"]) == ("Anand", "management")
    lines = answer.text.splitlines()
    at = lines.index("**What the filings say**")
    assert lines[at + 1] == "- Management said costs rose by 150 basis points [N1]."
    assert lines[at + 2] == f"  > “{COST_QUOTE}”"  # the quote under the sentence
    (row,) = log_rows()
    assert row.model == "fake-llm" and row.prompt_version == get_settings().llm.prompt_version
    post = json.loads(row.postcheck_json)
    assert post["kept"][0]["quote"] == COST_QUOTE and post["status"] == "llm"
    assert [d["reason"] for d in post["dropped"]] == ["no_passage"]


def test_a_number_the_model_adds_does_not_survive(data, monkeypatch) -> None:
    chunks_from(monkeypatch, {"transcript": [hit(COST_QUOTE, "CHK-1")]})
    llm = FakeLLM(
        default=claims_json(
            claim("Management said costs rose by 250 basis points.", ["N1"], COST_QUOTE),
            claim("Management said costs rose.", ["N1"], COST_QUOTE),
            claim("Management said margin fell by 2 points.", ["N1"], COST_QUOTE),
        )
    )
    answer = make_service(llm).ask(ASK, TODAY)
    assert answer.explanation == "Management said costs rose [N1]."
    assert [d["reason"] for d in answer.dropped] == [
        "number_not_in_quote: 250",
        "number_not_in_quote: 2",
    ]
    assert any("2 claim(s)" in w for w in answer.warnings)


def test_everything_dropped_falls_back_to_the_standard_sentence(data, monkeypatch) -> None:
    chunks_from(monkeypatch, {"transcript": [hit(COST_QUOTE, "CHK-1")]})
    llm = FakeLLM(default=claims_json(claim("Margin was great.", [], "")))
    answer = make_service(llm).ask(ASK, TODAY)
    assert answer.explanation == NO_EXPLANATION and answer.dropped
    assert "**What the filings say**\nThe filings do not explain this." in answer.text


def test_acceptance_6_no_chunks_means_numbers_block_and_no_llm_call(data, monkeypatch) -> None:
    chunks_from(monkeypatch, {})
    llm = FakeLLM(default="never")
    answer = make_service(llm).ask(ASK, TODAY)
    assert answer.explanation == NO_CHUNKS == NO_EXPLANATION == "The filings do not explain this."
    assert "14.49%" in answer.numbers_block and llm.calls == []
    assert any("No earnings-call transcript is indexed" in w for w in answer.warnings)
    assert any("No passage from the indexed filings was usable" in w for w in answer.warnings)


def test_acceptance_8_server_down_gives_the_numbers_with_a_warning(data, monkeypatch) -> None:
    chunks_from(monkeypatch, {"transcript": [hit("Costs rose.", "CHK-1")]})
    answer = make_service(FakeLLM(default="x", available=False)).ask(ASK, TODAY)
    assert isinstance(answer, Answer) and "14.49%" in answer.numbers_block
    assert LLM_DOWN_NOTE in answer.warnings and answer.explanation == ""


def test_acceptance_8_connection_refused_during_the_call(data, monkeypatch) -> None:
    from finrag.llm.base import LLMUnavailable

    class Flaky(FakeLLM):
        def generate(self, system, prompt, json_schema=None):
            raise LLMUnavailable("connection refused")

    chunks_from(monkeypatch, {"transcript": [hit("Costs rose.", "CHK-1")]})
    answer = make_service(Flaky(default="x")).ask(ASK, TODAY)
    assert isinstance(answer, Answer) and any("not available" in w for w in answer.warnings)


def test_a_reply_that_is_not_json_claims_gives_the_figures_and_says_so(data, monkeypatch) -> None:
    chunks_from(monkeypatch, {"transcript": [hit(COST_QUOTE, "CHK-1")]})
    answer = make_service(FakeLLM(default="Costs rose [N1].")).ask(ASK, TODAY)
    assert "14.49%" in answer.numbers_block and answer.explanation == ""
    assert any("could not be read as claims" in w for w in answer.warnings)


def test_results_text_fills_the_slots_and_the_transcript_is_flagged_missing(
    data, monkeypatch
) -> None:
    chunks_from(
        monkeypatch,
        {
            "results": [
                hit(
                    NOTE_QUOTE,
                    "CHK-2",
                    source="results",
                    section="results_notes",
                    speaker=None,
                    role=None,
                    page=9,
                )
            ]
        },
    )
    llm = FakeLLM(
        default=claims_json(claim("Note 5 changed how inventory is valued.", ["N1"], NOTE_QUOTE))
    )
    answer = make_service(llm).ask(ASK, TODAY)
    assert answer.explanation == "Note 5 changed how inventory is valued [N1]."
    assert any("No earnings-call transcript was available" in w for w in answer.warnings)
    cite = next(c for c in answer.citations if c.alias == "N1")
    assert cite.detail["source_type"] == "results" and cite.detail["page"] == 9


def test_question_on_an_unindexed_source_says_so(data, monkeypatch) -> None:
    chunks_from(monkeypatch, {})
    llm = FakeLLM(default="never")
    answer = make_service(llm).ask("What are Gabriel's contingent liabilities?", TODAY)
    assert isinstance(answer, Answer) and llm.calls == []
    assert any("contingent liabilities" in w and "not indexed" in w for w in answer.warnings)
    assert answer.explanation == ""


# --- not computed: acceptance 7 --------------------------------------------------------------


def test_acceptance_7_a_blocked_metric_shows_why(kit) -> None:
    kit.metric(
        GABRIEL,
        "cfo_to_pat",
        "FY26",
        None,
        status="not_computed",
        reason="Blocked by validation: cash_ties_to_bs.",
        details={"code": "blocked_by_validation", "blocked_by": ["cash_ties_to_bs"]},
    )
    answer = make_service().ask("What was Gabriel's cash conversion in FY26?", TODAY)
    assert "not computed — Blocked by validation: cash_ties_to_bs." in answer.text
    assert "Blocked by validation rule: cash_ties_to_bs." in answer.text


def test_hdfc_cost_to_income_defaults_to_standalone_and_offers_consolidated(kit) -> None:
    kit.metric(HDFC, "cost_to_income", "FY26", D("0.37999"), basis="standalone")
    kit.metric(HDFC, "cost_to_income", "FY26", D("0.58449"), basis="consolidated")
    answer = make_service().ask("HDFC cost to income FY26", TODAY)
    assert "standalone basis (preferred for this metric)" in answer.interpretation
    assert "Cost to income ratio, FY26 (Apr 2025–Mar 2026), standalone: 38.00%" in answer.text
    assert (
        "Also on the other basis: [F2] Cost to income ratio, FY26 (Apr 2025–Mar 2026), "
        "consolidated: 58.45%" in answer.text
    )
