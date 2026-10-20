"""M09.1: the explain step selects numbered sentences; the reader sees the stored sentence."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from finrag.core.config import get_settings, reset_settings_cache
from finrag.llm.fake import FakeLLM
from finrag.query.models import Answer
from finrag.query.postcheck import NO_EXPLANATION
from finrag.query.sentences import split_sentences

from .conftest import GABRIEL, TODAY
from .test_step7_service import chunks_from, hit, log_rows, make_service

pytestmark = [pytest.mark.usefixtures("seeded"), pytest.mark.selection]
D = Decimal

ASK = "Why did Gabriel's PAT jump in Q1 FY27?"
S1 = "The Board approved a Composite Scheme of Arrangement with Anchemco India Private Limited."
S2 = (
    "The Scheme has been approved by the National Company Law Tribunal vide its order dated "
    "May 11, 2026."
)
S3 = (
    "Accordingly, the profit of the Company reflects the Scheme using the pooling of interest "
    "method, applied retrospectively for all periods and year ended March 31, 2026."
)
S4 = "Profit for the period includes the share of profit of the associates."
SCHEME = f"{S1} {S2} {S3} {S4}"
RELATED_HEADING = "Related disclosures (not summarised; not stated as the reason for the change)"
ABBREVIATIONS = frozenset({"rs", "no", "ltd"})


def reply(*picks: tuple[str, str]) -> str:
    return json.dumps(
        {"selections": [{"id": i, **({"lead_in": lead} if lead else {})} for i, lead in picks]}
    )


def strong(h, dense=0.7):
    return replace(h, scores={"rrf": 0.03, "bm25": 7.0, "dense": dense})


def results_hit(text=SCHEME, label="Q1 FY27", dense=0.7, cid="CHK-R1"):
    return strong(hit(text, cid, source="results", section="results_notes", label=label), dense)


def set_lead_ins(isolated_repo, value: bool) -> None:
    path = isolated_repo / "config" / "settings.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "show_lead_ins: false", f"show_lead_ins: {str(value).lower()}"
        ),
        encoding="utf-8",
    )
    reset_settings_cache()


@pytest.fixture(autouse=True)
def lead_ins(isolated_repo, request) -> None:
    """Lead-ins are off by default; the tests of their checks turn them on."""
    if not request.node.name.startswith("test_default"):
        set_lead_ins(isolated_repo, True)


@pytest.fixture
def pat(kit, monkeypatch):
    kit.fact(GABRIEL, "profit_after_tax", "Q1 FY27", D("1081260000"))
    return monkeypatch


def ask(llm, question=ASK):
    answer = make_service(llm).ask(question, TODAY)
    assert isinstance(answer, Answer)
    return answer


# --- the split ---------------------------------------------------------------------------------


def test_sentences_are_split_without_cutting_abbreviations_labels_or_list_numbers() -> None:
    text = (
        "Revenue was Rs. 3,512.88 million for the year. It grew 5% on the base. "
        "4. Based on our review nothing came up."
    )
    assert split_sentences(text, ABBREVIATIONS) == [
        "Revenue was Rs. 3,512.88 million for the year.",
        "It grew 5% on the base.",
        "Based on our review nothing came up.",
    ]
    qa = "Answer (Asha Rao, Chief Financial Officer) (continued): We grew well. Margins held up."
    assert split_sentences(qa, ABBREVIATIONS) == ["We grew well.", "Margins held up."]


# --- the pick is shown as stored ---------------------------------------------------------------


def test_the_prompt_numbers_the_sentences_and_the_reader_sees_the_stored_one(pat) -> None:
    chunks_from(pat, {"transcript": [], "results": [results_hit()]})
    llm = FakeLLM(default=reply(("N1.1", "On the accounting for the scheme")))
    answer = ask(llm)
    prompt = llm.calls[0][1]
    assert f"N1.1 {S1}" in prompt and f"N1.3 {S3}" in prompt
    assert "On the accounting for the scheme [N1]" in answer.text
    assert f"  > “{S1}”" in answer.text  # the stored sentence, byte for byte
    assert answer.explanation and "The filings do not explain this" not in answer.text


def test_default_output_is_the_source_label_and_the_stored_sentence_in_the_models_order(
    pat,
) -> None:
    chunks_from(pat, {"transcript": [], "results": [results_hit()]})
    llm = FakeLLM(default=reply(("N1.4", ""), ("N1.1", "")))
    answer = ask(llm)
    label = "- Results filing, results notes, Q1 FY27, p. 5 [N1]:"
    shown = answer.text.split("**What the filings say**")[1].split("**Sources**")[0]
    assert shown.strip().splitlines() == [label, f"  > “{S4}”", label, f"  > “{S1}”"]
    assert "lead-in" not in answer.text
    system, prompt, schema = llm.calls[0]
    assert "lead_in" not in prompt and "lead_in" not in json.dumps(schema)


# --- lead-ins ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lead_in, reason",
    [
        ("It added 12 crore to profit", "number_not_in_sentence"),
        ("In Q4 FY26 the scheme was approved", "period_not_in_sentence"),
        ("The Reliance scheme", "entity_not_in_sentence"),
        ("Profit rose due to the scheme", "causal_word_not_in_sentence"),
    ],
)
def test_a_lead_in_with_anything_not_in_its_sentence_is_dropped_and_the_bare_sentence_shown(
    pat, lead_in, reason
) -> None:
    chunks_from(pat, {"transcript": [], "results": [results_hit()]})
    answer = ask(FakeLLM(default=reply(("N1.1", lead_in))))
    assert lead_in not in answer.text and f"  > “{S1}”" in answer.text
    assert any(w.startswith("1 lead-in(s) were removed") and reason in w for w in answer.warnings)


def test_a_lead_in_may_use_what_its_sentence_or_a_shown_figure_contains(pat) -> None:
    chunks_from(pat, {"transcript": [], "results": [results_hit()]})
    for lead_in in ("On the scheme with Anchemco", "Profit was 108.13 crore, and the scheme"):
        answer = ask(FakeLLM(default=reply(("N1.1", lead_in))))
        assert lead_in in answer.text and not any("lead-in(s)" in w for w in answer.warnings)


def test_a_hedged_sentence_needs_a_hedged_lead_in(monkeypatch) -> None:
    text = "So maybe around 40% to 45% should be around on the AI side of it."
    turn = strong(hit(text, "CHK-T1", source="transcript", section="prepared_remarks"))
    chunks_from(monkeypatch, {"transcript": [turn], "results": []})
    q = "What did Gabriel management say about the order book?"
    flat = ask(FakeLLM(default=reply(("N1.1", "The AI share is 40% to 45%"))), q)
    assert "The AI share is 40% to 45%" not in flat.text
    assert any("hedge_not_kept" in w for w in flat.warnings)
    kept = ask(FakeLLM(default=reply(("N1.1", "Around 40% to 45% on the AI side"))), q)
    assert "Around 40% to 45% on the AI side" in kept.text


def test_management_in_a_lead_in_needs_a_management_turn_and_an_analyst_is_named_as_one(
    monkeypatch,
) -> None:
    text = "Do you think margins have bottomed out for the year now?"
    turn = strong(
        hit(
            text,
            "CHK-T1",
            source="transcript",
            section="qa",
            speaker="Ravi Menon",
            role="analyst",
            firm="Alpha Capital",
        )
    )
    chunks_from(monkeypatch, {"transcript": [turn], "results": []})
    answer = ask(
        FakeLLM(default=reply(("N1.1", "Management said margins bottomed out"))),
        "What did Gabriel management say about margins?",
    )
    assert "Management said margins bottomed out" not in answer.text
    assert any("management_not_speaking" in w for w in answer.warnings)
    assert "- An analyst (Ravi Menon, Alpha Capital) [N1]:" in answer.text
    assert f"  > “{text}”" in answer.text


# --- the sentence rules ------------------------------------------------------------------------


def test_the_period_rules_apply_to_the_selected_sentence(pat) -> None:
    chunks_from(pat, {"transcript": [], "results": [results_hit()]})
    answer = ask(FakeLLM(default=reply(("N1.1", ""), ("N1.3", ""), ("N1.9", ""), ("N1.1", ""))))
    shown = answer.text.split("**Sources**")[0]
    assert f"“{S1}”" in shown and S3 not in shown  # S3 names the year ended March 31, 2026
    reasons = {d["reason"].split(":")[0] for d in answer.dropped}
    assert reasons == {"quote_other_period", "unknown_sentence", "duplicate_sentence"}


def test_a_sentence_of_a_passage_from_another_period_is_dropped(pat) -> None:
    old = replace(results_hit(), period_start="2025-04-01", period_end="2025-06-30")
    chunks_from(pat, {"transcript": [], "results": [old]})
    answer = ask(FakeLLM(default=reply(("N1.1", ""))))
    assert [d["reason"].split(":")[0] for d in answer.dropped] == ["passage_other_period"]


# --- nothing selected --------------------------------------------------------------------------


def test_when_no_selection_survives_but_retrieval_is_strong_related_disclosures_are_shown(
    pat,
) -> None:
    chunks_from(pat, {"transcript": [], "results": [results_hit(dense=0.7)]})
    answer = ask(FakeLLM(default=reply()))
    assert f"**{RELATED_HEADING}**" in answer.text
    assert "The filings do not explain this" not in answer.text
    assert answer.text.count("  > “") == get_settings().query.retrieval.related_sentences
    assert "[N1]" in answer.text and any(c.alias == "N1" for c in answer.citations)


def test_when_retrieval_is_weak_or_empty_the_standard_sentence_stays(pat) -> None:
    chunks_from(pat, {"transcript": [], "results": [results_hit(dense=0.3)]})
    weak = ask(FakeLLM(default=reply()))
    assert weak.explanation == NO_EXPLANATION and RELATED_HEADING not in weak.text
    chunks_from(pat, {"transcript": [], "results": []})
    empty = ask(FakeLLM(default="never"))
    assert empty.explanation == NO_EXPLANATION and RELATED_HEADING not in empty.text


# --- the raw reply -----------------------------------------------------------------------------


def test_the_raw_reply_is_logged_behind_a_settings_flag(pat, isolated_repo) -> None:
    chunks_from(pat, {"transcript": [], "results": [results_hit()]})
    raw = reply(("N1.1", "On the scheme"))
    ask(FakeLLM(default=raw))
    logged = json.loads(log_rows()[-1].postcheck_json)
    assert logged["raw_reply"] == raw and logged["picked"][0]["id"] == "N1.1"
    path = isolated_repo / "config" / "settings.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "query_log_raw_reply: true", "query_log_raw_reply: false"
        ),
        encoding="utf-8",
    )
    reset_settings_cache()
    ask(FakeLLM(default=raw))
    assert "raw_reply" not in json.loads(log_rows()[-1].postcheck_json)
