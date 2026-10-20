"""Evidence-bound claims, period binding, no auditor text for "why", speaker attribution and the
lookup default (the five faults found in the first live runs with Qwen3-4B), and the replays of the
three live answers.

The replays use the claims a model would have returned for the three logged answers
(QRY-01M5AZFF...,
QRY-01M5AZGF..., QRY-01M5AZH4...): the sentences are the logged ones, the quotes are copied from the
passages the model had. The passage texts are excerpts of the stored chunks.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from finrag.core.config import get_settings
from finrag.core.fiscal import label_to_period
from finrag.llm.fake import FakeLLM
from finrag.query.claims import Claim, ClaimRules, check_claim
from finrag.query.intents import load_intents
from finrag.query.models import Answer, Clarification, Intent
from finrag.query.postcheck import NO_EXPLANATION

from .conftest import GABRIEL, HDFC, TODAY, claims_json
from .test_step4_parser import make_parser
from .test_step6_answer import hit, make_intent
from .test_step7_service import make_service

pytestmark = pytest.mark.usefixtures("seeded")
D = Decimal


def P(label):
    return label_to_period(label)


def rules(periods=("Q1 FY27",), bind_period=True) -> ClaimRules:
    words = load_intents()
    return ClaimRules(
        periods=tuple(P(p) for p in periods),
        bind_period=bind_period,
        causal_words=tuple(words.causal_words),
        management_terms=tuple(words.speaker_terms["management"]),
        analyst_terms=tuple(words.speaker_terms["analyst"]),
        management_roles=frozenset(words.management_speaker_roles),
        min_quote_chars=get_settings().query.answer.min_quote_chars,
    )


def check(text, cites, quote, passages, figures=None, **kw) -> str | None:
    """The reason a claim is dropped (None = kept)."""
    return check_claim(Claim(text, tuple(cites), quote), passages, figures or {}, rules(**kw))


def call(text, role="management", **kw):
    return hit(text, speaker="Speaker", role=role, **kw)


def results(text, **kw):
    return hit(text, source="results", section="results_notes", speaker=None, role=None, **kw)


# --- 1. evidence-bound claims ----------------------------------------------------------------

PASSAGE = (
    "Raw material costs rose by 150 basis points  in the quarter, and the   company raised\n"
    "prices in July.  Margins were helped by mix."
)


def test_the_quote_must_be_a_verbatim_substring_of_a_cited_passage() -> None:
    p = {"N1": call(PASSAGE), "N2": results("Price increases helped to offset costs.")}
    quote = "Raw material costs rose by 150 basis points in the quarter"  # whitespace differs
    assert check("Costs rose by 150 basis points.", ["N1"], quote, p) is None
    altered = "Raw material costs went up by 150 basis points in the quarter"
    assert check("Costs rose by 150 basis points.", ["N1"], altered, p) == "quote_not_in_passage"
    assert check("Costs rose by 150 basis points.", ["N2"], quote, p) == "quote_not_in_passage"
    assert check("Costs rose.", ["N1"], "Margins were", p) == "quote_too_short"
    assert check("Costs rose.", ["N1"], "", p) == "quote_too_short"


def test_a_number_in_the_text_must_be_in_the_quote_or_equal_a_cited_figure() -> None:
    p = {"N1": call("Raw material costs rose by 150 basis points. Capex was 40 crore this year.")}
    quote = "Raw material costs rose by 150 basis points."
    assert check("Costs rose by 150 basis points.", ["N1"], quote, p) is None
    # 40 is printed in the passage but not in the quote: the old "numbers of the passage" rule
    # is gone
    assert check("Costs rose; capex was 40 crore.", ["N1"], quote, p) == "number_not_in_quote: 40"
    # a figure's displayed value is allowed when the claim cites that figure
    figures = {"F1": {D("14.49")}}
    assert check("The margin was 14.49%.", ["N1", "F1"], quote, p, figures) is None
    assert (
        check("The margin was 14.49%.", ["N1"], quote, p, figures) == "number_not_in_quote: 14.49"
    )


@pytest.mark.parametrize(
    "word",
    ["due to", "because", "driven by", "on account of", "led to", "owing to", "as a result of"],
)
def test_a_causal_word_in_the_text_needs_a_causal_marker_in_the_quote(word) -> None:
    states = "Other income was higher in the quarter than a year ago."
    says = f"Other income was higher in the quarter {word} treasury gains."
    p = {"N1": call(states + " " + says)}
    assert check(f"Profit rose {word} treasury gains.", ["N1"], says, p) is None
    assert (
        check(f"Profit rose {word} treasury gains.", ["N1"], states, p)
        == "causal_word_not_in_quote"
    )
    assert check("Profit rose with treasury gains.", ["N1"], states, p) is None  # no cause claimed


def test_a_claim_needs_a_passage_not_only_a_figure() -> None:
    p = {"N1": call(PASSAGE)}
    assert check("The margin was 14.49%.", ["F1"], PASSAGE, p, {"F1": {D("14.49")}}) == "no_passage"


# --- 2. period binding -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "quote,reason",
    [
        (
            "The business made a net profit of Rs. 12 million for the year ended March 31, 2026.",
            True,
        ),
        (
            "The business made a net profit of Rs. 12 million for the quarter ended June 30, 2025.",
            True,
        ),
        ("The business made a net profit of Rs. 12 million in Q4 FY26.", True),
        ("The business made a net profit of Rs. 12 million in FY25.", True),
        ("The business had a net profit of Rs. 12 million as at March 31, 2026.", True),
        (
            "The business made a net profit of Rs. 12 million for the quarter ended June 30, 2026.",
            False,
        ),
        ("The business had a net profit of Rs. 12 million as at June 30, 2026.", False),
        ("The business made a net profit of Rs. 12 million in Q1 FY27.", False),
        ("The business made a net profit of Rs. 12 million this quarter.", False),
    ],
)
def test_a_quote_carrying_another_period_is_dropped_when_the_question_names_q1_fy27(quote, reason):
    p = {"N1": call(quote)}
    got = check("The business made a net profit of Rs. 12 million.", ["N1"], quote, p)
    assert (got is not None and got.startswith("quote_other_period")) is reason, got


def test_a_passage_from_another_period_is_dropped() -> None:
    q4 = replace(
        call("Gross margin improved on a better mix of products."),
        period_start="2026-01-01",
        period_end="2026-03-31",
        period_label="Q4 FY26",
    )
    p = {"N1": q4, "N2": call("Gross margin improved on a better mix of products.")}
    text, quote = "Gross margin improved on mix.", "Gross margin improved on a better mix"
    assert check(text, ["N1"], quote, p) == "passage_other_period: Q4 FY26"
    assert check(text, ["N2"], quote, p) is None


def test_a_fiscal_year_question_accepts_its_quarters_and_rejects_other_years() -> None:
    q3 = replace(
        call("Order inflow was strong in the third quarter of the year."),
        period_start="2025-10-01",
        period_end="2025-12-31",
        period_label="Q3 FY26",
    )
    p = {"N1": q3}
    quote = "Order inflow was strong in the third quarter"
    assert check("Orders were strong.", ["N1"], quote, p, periods=("FY26",)) is None
    assert check("Orders were strong.", ["N1"], quote, p, periods=("Q1 FY27",)) is not None
    prior = {
        "N1": replace(
            call("Revenue for the year ended March 31, 2025 was strong overall."),
            period_start="2025-10-01",
            period_end="2025-12-31",
            period_label="Q3 FY26",
        )
    }
    got = check(
        "Revenue was strong.",
        ["N1"],
        "Revenue for the year ended March 31, 2025 was strong",
        prior,
        periods=("FY26",),
    )
    assert got == "quote_other_period: year ended March 31, 2025"


def test_a_forward_looking_question_is_not_bound_to_a_period() -> None:
    quote = "We plan capex of 200 crore in FY28 and FY29 across both plants."
    p = {"N1": call(quote)}
    text = "Management plans capex of 200 crore."
    assert check(text, ["N1"], quote, p, bind_period=False) is None
    assert check(text, ["N1"], quote, p, bind_period=True).startswith("quote_other_period")


# --- 3. no auditor text for "why" ------------------------------------------------------------


def test_the_default_results_search_leaves_out_the_auditors_text() -> None:
    from finrag.metrics.registry import load_metrics
    from finrag.normalize.taxonomy import load_taxonomy
    from finrag.query.planner import Planner

    taxonomy = load_taxonomy()
    planner = Planner(load_metrics(taxonomy), taxonomy, load_intents(taxonomy), get_settings())
    why = planner.plan(make_intent(qtype="explanation"))
    sections = {s for r in why.searches for s in (r.sections or [])}
    assert sections == {"results_notes", "segment_notes", "press_release"}
    # a question about the audit still searches it
    audit = planner.plan(make_intent(metrics=[], qtype="narrative"))
    audit = planner.plan(
        replace(make_intent(metrics=[], qtype="narrative"), sections=["auditor_report"])
    )
    assert [r.sections for r in audit.searches] == [["auditor_report"]]
    opinion = planner.plan(
        replace(make_intent(metrics=[], qtype="narrative"), sections=["audit_opinion"])
    )
    assert [r.sections for r in opinion.searches] == [["audit_opinion"]]


def test_a_why_answered_only_by_auditor_text_is_the_figures_and_the_standard_sentence(
    kit, monkeypatch
) -> None:
    kit.metric(GABRIEL, "operating_margin", "Q1 FY27", D("0.14489"))
    seen = []

    def _search(request, embedder, settings=None, engine=None):
        seen.append(request)
        return []  # the index holds auditor text only, and that is not searched

    monkeypatch.setattr("finrag.query.retriever.search", _search)
    llm = FakeLLM(default="never")
    answer = make_service(llm).ask("Why did Gabriel's operating margin fall in Q1 FY27?", TODAY)
    assert all(
        not {"auditor_report", "audit_opinion"} & set(r.sections or []) for r in seen if r.sections
    )
    assert answer.explanation == NO_EXPLANATION and llm.calls == []
    assert "14.49%" in answer.numbers_block
    assert answer.text.splitlines()[-1] != "" and "The filings do not explain this." in answer.text


# --- 4. speaker attribution ------------------------------------------------------------------

ANALYST_Q = "Do you think margins have bottomed out now and what are the headwinds for margins?"
MGMT_A = (
    "The cost of funds is the biggest opportunity on the margin, "
    "though it will not change in a hurry."
)


def test_management_is_said_only_of_management_turns() -> None:
    p = {"N1": call(MGMT_A), "N2": call(ANALYST_Q, role="analyst"), "N3": results(MGMT_A)}
    said = "Management said the cost of funds is the biggest opportunity on the margin."
    assert check(said, ["N1"], MGMT_A, p) is None
    assert check(said, ["N2"], ANALYST_Q, p) == "management_not_speaking"
    assert check(said, ["N3"], MGMT_A, p) == "management_not_speaking"  # the filing, not a speaker
    assert (
        check("The filing says cost of funds is the biggest opportunity.", ["N3"], MGMT_A, p)
        is None
    )


def test_an_analysts_words_must_be_called_an_analysts() -> None:
    p = {"N1": call(ANALYST_Q, role="analyst")}
    assert (
        check("They asked if margins have bottomed out.", ["N1"], ANALYST_Q, p)
        == "analyst_not_named"
    )
    assert (
        check("An analyst asked whether margins have bottomed out.", ["N1"], ANALYST_Q, p) is None
    )


def test_a_figure_is_never_attributed_to_management() -> None:
    p = {"N1": call(MGMT_A)}
    figures = {"F1": {D("1.17")}}
    got = check("Management said the gross NPA ratio was 1.17%.", ["N1", "F1"], MGMT_A, p, figures)
    assert got == "figure_attributed_to_management"
    assert check("The gross NPA ratio was 1.17% [F1].", ["N1", "F1"], MGMT_A, p, figures) is None


def test_the_what_did_management_say_search_is_limited_to_management_turns(kit) -> None:
    kit.metric(HDFC, "gross_npa_ratio", "Q1 FY27", D("0.0117"), basis="standalone")
    service = make_service()
    intent, plan = service.explain_plan("What did HDFC management say about asset quality?", TODAY)
    assert intent.management_only and intent.question_type == "narrative"
    transcript = next(r for r in plan.searches if r.source_types == ["transcript"])
    results_ = next(r for r in plan.searches if r.source_types == ["results"])
    assert transcript.speaker_roles == ["management"] and results_.speaker_roles is None
    why = service.explain_plan("Why did HDFC GNPA change in Q1 FY27?", TODAY)[0]
    assert not why.management_only  # a plain "why" may use the whole call, each turn labelled


# --- 5. lookup default -----------------------------------------------------------------------


def test_a_metric_with_no_cue_is_a_lookup_figures_only(kit, monkeypatch) -> None:
    kit.metric(HDFC, "gross_npa_ratio", "Q1 FY27", D("0.0117"), basis="standalone")
    searched = []
    monkeypatch.setattr("finrag.query.retriever.search", lambda *a, **k: searched.append(a) or [])
    llm = FakeLLM(default="never")
    service = make_service(llm)
    intent, plan = service.explain_plan("HDFC GNPA latest", TODAY)
    assert intent.question_type == "lookup" and plan.searches == []
    answer = service.ask("HDFC GNPA latest", TODAY)
    assert isinstance(answer, Answer) and llm.calls == [] and searched == []
    assert "1.17%" in answer.text and answer.explanation == ""
    assert "What the filings say" not in answer.text and "What management said" not in answer.text


@pytest.mark.parametrize(
    "q",
    [
        "why did HDFC GNPA rise in Q1 FY27",
        "explain HDFC GNPA in Q1 FY27",
        "HDFC GNPA reason Q1 FY27",
        "HDFC GNPA drivers Q1 FY27",
        "what caused the HDFC GNPA change in Q1 FY27",
        "what drove HDFC GNPA in Q1 FY27",
        "how come HDFC GNPA moved in Q1 FY27",
        "HDFC GNPA commentary Q1 FY27",
        "what was said about HDFC GNPA in Q1 FY27",
        "did HDFC mention GNPA in Q1 FY27",
    ],
)
def test_every_explanation_or_commentary_cue_still_searches_the_narrative(q, kit) -> None:
    intent = make_parser().parse(q, TODAY)
    assert isinstance(intent, Intent) and intent.question_type in ("explanation", "narrative"), q


@pytest.mark.parametrize(
    "q,metric",
    [
        ("HDFC loans growth FY26", "advances_growth_yoy"),
        ("HDFC loan growth FY26", "advances_growth_yoy"),
        ("HDFC deposits growth FY26", "deposits_growth_yoy"),
        ("Netweb turnover growth FY26", "revenue_growth_yoy"),
        ("Gabriel profit after tax growth FY26", "pat_growth_yoy"),
    ],
)
def test_growth_resolves_to_the_year_on_year_growth_metric(q, metric) -> None:
    got = make_parser().parse(q, TODAY)
    assert isinstance(got, Intent) and got.metrics == [metric], q


def test_loans_without_growth_stays_the_level() -> None:
    got = make_parser().parse("HDFC loans FY26", TODAY)
    assert isinstance(got, Intent) and got.metrics == ["bank_advances"]


def test_nim_is_not_computable_so_the_clarification_stays() -> None:
    from finrag.metrics.registry import load_metrics
    from finrag.normalize.taxonomy import load_taxonomy

    taxonomy = load_taxonomy()
    registry = load_metrics(taxonomy)
    # NIM = net interest income / average interest-earning assets; no concept holds the assets
    assert not [m for m in registry.order() if "margin" in m and "interest" in m or m == "nim"]
    assert not [
        c for c in taxonomy.concepts if "earning" in c and "per_share" not in c and "eps" not in c
    ]
    got = make_parser().parse("HDFC NIM Q1 FY27", TODAY)
    assert isinstance(got, Clarification) and got.reason == "unknown_metric"
    assert "nii" in [c.id for c in got.candidates]  # net interest income is offered instead


# --- the replays of the three live answers ---------------------------------------------------

GABRIEL_P9_9 = (
    "6. The comparative figures of the Group as set out in the Statement includes the financial "
    "information for the year ended March 31, 2026, related to business operations of Anchemco "
    "India Private Limited (hereinafter referred to as the ‘Business’) and one associate which "
    "has been transferred to the Group pursuant to the Scheme of Arrangement approved by the "
    "National Company Law Tribunal (‘NCLT’) vide its order dated May 11, 2026. The financial "
    "information of the business reflects total revenues of Rs. 3,512.88 million, total net profit "
    "after tax of Rs. 153.16 million and total comprehensive income of Rs. 151.68 million for the "
    "year ended March 31, 2026. In respect of one associate, the Consolidated financial results "
    "include the Group’s share of net profit after tax of Rs."
)
GABRIEL_P9_10 = (
    "153.16 million and total comprehensive income of Rs. 151.68 million for the year ended March "
    "31, 2026. 7. The consolidated unaudited financial results include the Group’s share of net "
    "profit after tax of Rs. 280.12 million and total comprehensive income of Rs. 280.12 million "
    "for the quarter ended June 30, 2026 in respect of one associate. These financial information "
    "have been reviewed by other auditors and their reports July 20, 2026, in which they have "
    "issued an unmodified conclusion, have been furnished to us by the auditors."
)


def auditor(text, cid):
    return replace(results(text, cid=cid, page=9), section="auditor_report")


def test_replay_gabriel_pat_jump_has_no_280_153_or_due_to_claim() -> None:
    p = {"N2": auditor(GABRIEL_P9_9, "p9-9"), "N4": auditor(GABRIEL_P9_10, "p9-10")}
    figures = {"F1": {D("108.13")}}
    claims = [
        Claim(  # the logged first sentence, with the quote the model would give for it
            "The consolidated profit for Q1 FY27 increased due to the inclusion of the Group’s "
            "share of net profit after tax from an associate, which contributed Rs. 280.12 million "
            "for the quarter ended June 30, 2026.",
            ("N4",),
            "The consolidated unaudited financial results include the Group’s share of net "
            "profit after tax of Rs. 280.12 million and total comprehensive income of Rs. 280.12 "
            "million for the quarter ended June 30, 2026 in respect of one associate.",
        ),
        Claim(
            "The profit also includes the impact of business operations from Anchemco India "
            "Private "
            "Limited transferred to the Group under a Scheme of Arrangement, which contributed Rs. "
            "153.16 million in net profit after tax for the year ended March 31, 2026.",
            ("N2",),
            "total net profit after tax of Rs. 153.16 million and total comprehensive income of "
            "Rs. 151.68 million for the year ended March 31, 2026",
        ),
    ]
    got = [check_claim(c, p, figures, rules()) for c in claims]
    assert (
        got[0] == "causal_word_not_in_quote"
    )  # "due to": the auditor's paragraph says no such thing
    assert got[1] == "quote_other_period: year ended March 31, 2026"  # a comparative, not Q1 FY27
    # and neither can come back in a different wording: the auditor's text is no longer searched
    # for a "why" (tested above), so these passages never reach the model.


def test_replay_gabriel_end_to_end_the_auditor_passages_are_not_in_the_prompt(
    kit, monkeypatch
) -> None:
    kit.metric(GABRIEL, "profit_after_tax", "Q1 FY27", D("1081260000"))
    sections_asked = []

    def _search(request, embedder, settings=None, engine=None):
        sections_asked.append(request.sections)
        return []

    monkeypatch.setattr("finrag.query.retriever.search", _search)
    llm = FakeLLM(default=claims_json())
    answer = make_service(llm).ask("Why did Gabriel's PAT jump in Q1 FY27?", TODAY)
    assert isinstance(answer, Answer)
    assert not any(
        {"auditor_report", "audit_opinion"} & set(s) for s in sections_asked if s is not None
    )
    assert "280.12" not in answer.text and "153.16" not in answer.text
    assert "due to" not in answer.explanation


HDFC_ANALYST_P12_29 = (
    "Question (Abhishek Murarka, HSBC): Yes, good evening and thanks for taking my question. "
    "So I'll "
    "just squeeze in few short direct questions. One, can you quantify how much of the bonds are "
    "maturing this year and probably this quarter? And the third is on ECL, can you quantify what "
    "will be the one-time impact and also on an ongoing basis how much would your credit cost be "
    "impacted? Answer (Srinivasan Vaidyanathan, Chief Financial Officer) (continued): "
    "And for us to "
    "think about the stage 3 assets which is equivalent to the NPA today that you have, that "
    "coverage is quite adequate there. Stage 1 and stage 2 which are in various buckets of "
    "delinquencies, the floors that are there, for example, the floor in the unsecured category is "
    "1% and the floor in the secured category is 5%."
)
HDFC_ANALYST_P4_6 = (
    "Question (Mahrukh Adajania, Tara Capital Partners): Yes, hi, good evening. My first question "
    "is on margins. Do you think margins have bottomed out now? Answer (Srinivasan Vaidyanathan, "
    "Chief Financial Officer) (continued): The retail deposit costs, us as well as various players "
    "in the industry, have been circumspect and have been steady there. But the non-retail deposit "
    "costs have been elevated."
)


def test_replay_hdfc_management_say_does_not_attribute_analyst_turns_to_management() -> None:
    p = {
        "N2": hit(HDFC_ANALYST_P12_29, speaker="Abhishek Murarka", role="analyst", firm="HSBC"),
        "N1": hit(HDFC_ANALYST_P4_6, speaker="Mahrukh Adajania", role="analyst", firm="Tara"),
    }
    figures = {"F1": {D("1.17")}}
    floors = (
        "stage 3 assets which is equivalent to the NPA today that you have, that coverage is quite "
        "adequate there. Stage 1 and stage 2 which are in various buckets of delinquencies, the "
        "floors that are there, for example, the floor in the unsecured category is 1% and the "
        "floor in the secured category is 5%"
    )
    elevated = "the non-retail deposit costs have been elevated"
    replay = [  # the four logged sentences
        (
            Claim(
                "HDFC management stated that the gross NPA ratio for Q1 FY27 was 1.17% "
                "on a standalone basis.",
                ("F1",),
                "1.17%",
            ),
            "no_passage",
        ),
        (
            Claim(
                "They indicated that stage 3 assets have adequate coverage and stage 1 and 2 "
                "have floors of 1% and 5%.",
                ("N2",),
                floors,
            ),
            "analyst_not_named",
        ),
        (
            Claim(
                "The management expressed confidence in the adequacy of reserves.", ("N2",), floors
            ),
            "management_not_speaking",
        ),
        (
            Claim(
                "They also noted that margin pressures are influenced by cost of funds, with "
                "non-retail deposit costs being elevated.",
                ("N1",),
                elevated,
            ),
            "analyst_not_named",
        ),
    ]
    for c, reason in replay:
        assert check_claim(c, p, figures, rules()) == reason, c.text
    # the same passages are fine when the claim says whose words they are
    said = Claim(
        "An analyst asked whether margins have bottomed out.",
        ("N1",),
        "Do you think margins have bottomed out now?",
    )
    assert check_claim(said, p, figures, rules()) is None


def test_replay_hdfc_gnpa_latest_makes_no_llm_call(kit, monkeypatch) -> None:
    kit.metric(HDFC, "gross_npa_ratio", "Q1 FY27", D("0.0117"), basis="standalone")
    monkeypatch.setattr("finrag.query.retriever.search", lambda *a, **k: pytest.fail("searched"))
    llm = FakeLLM(default="never")
    answer = make_service(llm).ask("HDFC GNPA latest", TODAY)
    assert isinstance(answer, Answer) and llm.calls == []
    assert [c.alias for c in answer.citations] == ["F1"]
    assert json.loads(claims_json())["claims"] == []  # (helper sanity)
