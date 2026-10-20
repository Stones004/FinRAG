"""Step 6: formatter, numbers block, answerer and post-check (M09 acceptance 4-7, 10, 12, 13)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from finrag.core.config import get_settings
from finrag.core.fiscal import label_to_period
from finrag.llm.fake import FakeLLM
from finrag.metrics.registry import load_metrics
from finrag.narrative.search import SearchHit
from finrag.normalize.taxonomy import load_taxonomy
from finrag.query.answerer import (
    LLM_DOWN,
    LLM_DRAFT,
    LLM_UNREADABLE,
    NO_CHUNKS_FOUND,
    NOT_NEEDED,
    UNAVAILABLE_SOURCE,
    Answerer,
    passage_label,
)
from finrag.query.claims import CLAIMS_SCHEMA, ClaimRules, parse_claims
from finrag.query.formatter import Formatter, numbers_in
from finrag.query.intents import load_intents
from finrag.query.models import Intent
from finrag.query.numbers_block import NumbersBlockBuilder
from finrag.query.planner import Planner
from finrag.query.postcheck import (
    NO_CHUNKS,
    NO_EXPLANATION,
    build_citations,
    chunk_header,
    postcheck,
)
from finrag.query.retriever import Retriever

from .conftest import GABRIEL, HDFC, claim, claims_json

pytestmark = pytest.mark.usefixtures("seeded")
D = Decimal


def P(label):
    return label_to_period(label)


def make_intent(
    metrics=("operating_margin",),
    periods=("Q1 FY27",),
    qtype="lookup",
    basis="consolidated",
    comparison=None,
    unavailable=(),
    isin=GABRIEL,
) -> Intent:
    return Intent(
        question="why did operating margin change",
        entity_isin=isin,
        entity_name="Gabriel",
        metrics=list(metrics),
        periods=[P(p) for p in periods],
        comparison=comparison,
        question_type=qtype,
        basis=basis,
        sections=[],
        confidence=1.0,
        decision="proceed",
        unavailable_sources=list(unavailable),
        defaults_applied=["basis:entity_default"],
    )


class Env:
    def __init__(self):
        self.settings = get_settings()
        self.taxonomy = load_taxonomy()
        self.registry = load_metrics(self.taxonomy)
        self.intents = load_intents(self.taxonomy)
        self.planner = Planner(self.registry, self.taxonomy, self.intents, self.settings)
        self.retriever = Retriever(None, self.settings, self.registry, self.taxonomy)
        self.fmt = Formatter(self.settings, self.registry, self.taxonomy)
        self.builder = NumbersBlockBuilder(
            self.fmt, self.registry, self.taxonomy, self.settings.defaults.display_unit
        )

    def run(self, intent: Intent):
        plan = self.planner.plan(intent)
        evidence = self.retriever.retrieve(plan)
        return plan, evidence, self.builder.build(intent, plan, evidence)


@pytest.fixture
def env() -> Env:
    return Env()


def hit(
    text,
    source="transcript",
    label="Q1 FY27",
    page=5,
    section="qa",
    speaker="Anand",
    role="management",
    firm=None,
    cid="CHK-FIL-aaa-p5-1",
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


# --- formatter -------------------------------------------------------------------------------


def test_formats_by_display_kind(env) -> None:
    f = env.fmt
    assert f.by_display(D("0.14489"), "percent").text == "14.49%"
    assert f.by_display(D("0.012"), "percentage_points").text == "1.20 pp"
    assert f.by_display(D("3452740000"), "crore").text == "345.27 crore"
    assert f.by_display(D("52.912"), "days").text == "52.91 days"
    assert f.by_display(D("1.6636"), "times").text == "1.66x"
    assert f.by_display(D("1234567.891"), "number").text == "1,234,567.89"
    assert f.by_display(D("-0.005"), "percent").number == D("0.50")
    assert f.concept(D("0.0117"), "bank_gross_npa_ratio").text == "1.17%"
    assert f.concept(D("4435590000"), "cfo").text == "443.56 crore"


def test_changes_are_points_for_ratios_and_percent_for_amounts(env) -> None:
    assert env.fmt.change(D("0.1449"), D("0.1329"), "percent").text == "+1.20 pp"
    assert env.fmt.change(D("0.1329"), D("0.1449"), "percent").text == "-1.20 pp"
    assert env.fmt.change(D("110"), D("100"), "crore").text == "+10.00%"
    assert env.fmt.change(D("1"), D("0"), "crore") is None


def test_numbers_in_ignores_labels_years_and_aliases() -> None:
    text = "In FY26 (2025-26) and Q1 FY27, margin was 14.49% [F1] vs 1,234.5 on 9M, 4G, 5th."
    assert numbers_in(text) == [D("14.49"), D("1234.5")]


# --- numbers block: acceptance 4, 7, 10 ------------------------------------------------------


def test_block_states_basis_unit_and_each_value(env, kit) -> None:
    kit.metric(GABRIEL, "operating_margin", "Q1 FY27", D("0.1449"))
    kit.metric(GABRIEL, "operating_margin", "Q1 FY27", D("0.15"), basis="standalone")
    plan, ev, block = env.run(make_intent())
    assert block.text.splitlines() == [
        "**Figures** — consolidated basis (company default); amounts in crore.",
        "- [F1] Operating margin, Q1 FY27 (Apr–Jun 2026), consolidated: 14.49%",
        "    Also on the other basis: [F2] Operating margin, Q1 FY27 (Apr–Jun 2026), "
        "standalone: 15.00%",
    ]
    assert "[F1] Operating margin, Q1 FY27 (Apr–Jun 2026), consolidated: 14.49%" in block.text
    assert "consolidated basis (company default)" in block.text  # acceptance 10
    assert block.allowed["F1"] == {D("14.49")} and block.allowed["F2"] == {D("15.00")}


def test_every_number_of_the_block_is_a_formatted_stored_value(env, kit) -> None:
    values = {"Q1 FY27": D("0.14489"), "Q4 FY26": D("0.1301"), "Q3 FY26": D("0.1")}
    for label, value in values.items():
        kit.metric(GABRIEL, "operating_margin", label, value)
    intent = make_intent(
        periods=["Q1 FY27", "Q4 FY26", "Q3 FY26"],
        qtype="comparison",
        comparison="trend",
        basis="consolidated",
    )
    plan, ev, block = env.run(intent)
    shown = {t for t in (env.fmt.metric(v, "operating_margin").text for v in values.values())}
    for text in shown:
        assert text in block.text
    stored = {env.fmt.metric(v, "operating_margin").number for v in values.values()}
    assert stored <= block.allowed["F1"] | block.allowed["F2"] | block.allowed["F3"]


def test_not_computed_shows_the_reason_and_the_blocking_rule(env, kit) -> None:
    kit.metric(
        GABRIEL,
        "cfo_to_pat",
        "FY26",
        None,
        status="not_computed",
        reason="Blocked by validation: cash_ties_to_bs.",
        details={"code": "blocked_by_validation", "blocked_by": [{"rule_id": "cash_ties_to_bs"}]},
    )
    _, _, block = env.run(make_intent(metrics=["cfo_to_pat"], periods=["FY26"]))
    assert "not computed — Blocked by validation: cash_ties_to_bs." in block.text
    assert "Blocked by validation rule: cash_ties_to_bs." in block.text


def test_not_applicable_reason(env, kit) -> None:
    kit.metric(
        HDFC,
        "operating_margin",
        "FY26",
        None,
        status="not_computed",
        reason="Not applicable to bank companies; defined for general.",
        details={"code": "not_applicable"},
    )
    _, _, block = env.run(make_intent(periods=["FY26"], isin=HDFC))
    assert "not computed — Not applicable to bank companies; defined for general." in block.text


def test_empty_evidence_says_what_is_missing(env) -> None:
    _, ev, block = env.run(make_intent())
    assert "No data for Operating margin, Q1 FY27 (Apr–Jun 2026), consolidated " in block.text
    assert "(missing: never computed for this period)" in block.text
    assert ev.entries[0].alias is None
    empty = make_intent(periods=[])
    plan, ev, block = env.run(empty)
    assert "No data: no filed results were found for this company." in block.text


def test_yoy_change_line(env, kit) -> None:
    kit.metric(GABRIEL, "operating_margin", "Q1 FY27", D("0.1449"))
    kit.metric(GABRIEL, "operating_margin", "Q1 FY26", D("0.1329"))
    _, _, block = env.run(make_intent(qtype="comparison", comparison="yoy"))
    assert "Change vs Q1 FY26 (Apr–Jun 2025): 13.29% [F2] → +1.20 pp" in block.text
    assert D("1.20") in block.allowed["F1"] and D("13.29") in block.allowed["F1"]


def test_period_comparison_change_line(env, kit) -> None:
    kit.metric(GABRIEL, "operating_margin", "FY26", D("0.14"))
    kit.metric(GABRIEL, "operating_margin", "FY25", D("0.12"))
    _, _, block = env.run(
        make_intent(periods=["FY26", "FY25"], qtype="comparison", comparison="periods")
    )
    assert "Change vs FY25 [F2]: +2.00 pp" in block.text


def test_basis_change_restatement_and_warnings_are_shown(env, kit) -> None:
    old = kit.fact(GABRIEL, "cfo", "FY26", 4435590000, filing_id=kit.filing(GABRIEL, "FY26", "o"))
    new = kit.fact(
        GABRIEL,
        "cfo",
        "FY26",
        3452740000,
        filing_id=kit.filing(GABRIEL, "FY26", "r", revision=1, filed_on="2026-06-29"),
    )
    kit.metric(
        GABRIEL,
        "fcf",
        "FY26",
        D("1458690000"),
        details={
            "code": "computed",
            "basis_used": "consolidated",
            "basis_change": [
                {
                    "event_id": 1,
                    "event_type": "accounting_policy_change",
                    "event_date": "2025-04-01",
                    "citation": "Note 5, inventory valuation.",
                }
            ],
            "restatements": [
                {"concept": "cfo", "old_fact_id": old, "new_fact_id": new, "abs_diff": "982850000"}
            ],
            "warnings": [
                {"concept": "finance_cost", "period_end": "2026-03-31", "rule_id": "xbrl_vs_pdf"}
            ],
            "assumed_zero": [{"concept": "lease_liabilities", "period_end": "2025-03-31"}],
        },
    )
    _, _, block = env.run(make_intent(metrics=["fcf"], periods=["FY26"]))
    text = block.text
    assert (
        "Basis change: the periods straddle an accounting policy change effective 2025-04-01"
        in text
    )
    assert "(event 1)" in text and "Note 5, inventory valuation." in text
    assert "Restated input " in text and ": was 443.56 crore" in text
    assert "now 345.27 crore" in text and "revision filed 2026-06-29" in text
    assert "Validation warning (xbrl_vs_pdf) on finance_cost, period ending 2026-03-31." in text
    assert "Assumed 0 for lease_liabilities at 2025-03-31 (not reported)." in text
    assert {D("443.56"), D("345.27")} <= block.allowed["F1"]


def test_basis_fallback_is_stated(env, kit) -> None:
    kit.metric(
        HDFC,
        "gross_npa_ratio",
        "Q1 FY27",
        D("0.0117"),
        details={
            "code": "computed",
            "basis_requested": "consolidated",
            "basis_used": "standalone",
            "basis_fallback": "standalone",
        },
    )
    _, _, block = env.run(make_intent(metrics=["gross_npa_ratio"], isin=HDFC))
    assert "Shown on the standalone basis: the consolidated filings report none" in block.text


# --- answerer: acceptance 6, 8, 12 -----------------------------------------------------------


def draft(env, kit, intent, chunks=(), llm=None, transcripts_missing=False):
    kit.metric(GABRIEL, "operating_margin", "Q1 FY27", D("0.1449"))
    plan, ev, block = env.run(intent)
    ev.chunks = list(chunks)
    ev.chunk_aliases = {f"N{i}": h.chunk_id for i, h in enumerate(chunks, 1)}
    ev.transcripts_missing = transcripts_missing
    answerer = Answerer(llm, env.intents)
    return answerer.draft(intent, plan, ev, block), ev, block


def test_no_chunks_means_no_llm_call_and_the_standard_sentence(env, kit) -> None:
    llm = FakeLLM(default="should not be used")
    d, _, _ = draft(env, kit, make_intent(qtype="explanation"), llm=llm)
    assert d.status == NO_CHUNKS_FOUND and d.text == NO_CHUNKS == NO_EXPLANATION
    assert NO_EXPLANATION == "The filings do not explain this."
    assert any("No passage from the indexed filings was usable" in n for n in d.notes)
    assert llm.calls == []


def test_lookup_never_calls_the_llm(env, kit) -> None:
    llm = FakeLLM(default="x")
    d, _, _ = draft(env, kit, make_intent(qtype="lookup"), chunks=[hit("t")], llm=llm)
    assert d.status == NOT_NEEDED and llm.calls == []


def test_prompt_carries_the_block_the_period_and_labelled_passages(env, kit) -> None:
    reply = claims_json(claim("Margin rose on mix.", ["N1"], "Margin rose on mix."))
    llm = FakeLLM(default=reply)
    chunk = hit("Margin rose on mix.", speaker="Anand", role="management", firm="Gabriel")
    d, _, block = draft(env, kit, make_intent(qtype="explanation"), chunks=[chunk], llm=llm)
    assert d.status == LLM_DRAFT and [c.cites for c in d.claims] == [("N1",)]
    system, prompt, schema = llm.calls[0]
    assert schema == CLAIMS_SCHEMA  # the reply is asked for as JSON claims
    assert block.text in prompt
    assert "Period asked: Q1 FY27 (Apr–Jun 2026)" in prompt
    assert (
        "[N1] (earnings call; period Q1 FY27 (Apr–Jun 2026); speaker Anand; "
        "role management, Gabriel) Margin rose on mix."
    ) in prompt
    assert "why did operating margin change" in prompt
    assert d.response.prompt_version == "test"


def test_every_passage_is_labelled_with_what_it_is_its_period_and_its_speaker() -> None:
    call = hit("x", speaker="Abhishek", role="analyst", firm="HSBC")
    notes = hit("y", source="results", section="results_notes", speaker=None, role=None, page=9)
    assert passage_label(call) == (
        "earnings call; period Q1 FY27 (Apr–Jun 2026); speaker Abhishek; role analyst, HSBC"
    )
    assert passage_label(notes) == (
        "results filing, results notes; period Q1 FY27 (Apr–Jun 2026); page 9; no speaker"
    )


def test_a_reply_that_is_not_claims_gives_figures_only_with_a_note(env, kit) -> None:
    d, _, _ = draft(
        env, kit, make_intent(qtype="explanation"), chunks=[hit("t")], llm=FakeLLM(default="prose")
    )
    assert d.status == LLM_UNREADABLE and d.claims == [] and d.text is None
    assert any("could not be read as claims" in n for n in d.notes)


def test_llm_down_gives_figures_only_with_a_note(env, kit) -> None:
    d, _, _ = draft(
        env,
        kit,
        make_intent(qtype="explanation"),
        chunks=[hit("t")],
        llm=FakeLLM(default="x", available=False),
    )
    assert d.status == LLM_DOWN and d.text is None
    assert any("not available" in n for n in d.notes)
    d, _, _ = draft(env, kit, make_intent(qtype="explanation"), chunks=[hit("t")], llm=None)
    assert d.status == LLM_DOWN


def test_missing_transcripts_are_noted_when_results_text_is_used(env, kit) -> None:
    chunk = hit("Note 5 text", source="results", section="results_notes", speaker=None, role=None)
    d, _, _ = draft(
        env,
        kit,
        make_intent(qtype="explanation"),
        chunks=[chunk],
        llm=FakeLLM(default=claims_json()),
        transcripts_missing=True,
    )
    assert any("No earnings-call transcript was available" in n for n in d.notes)
    d, _, _ = draft(
        env,
        kit,
        make_intent(qtype="explanation"),
        transcripts_missing=True,
        llm=FakeLLM(default="x"),
    )
    assert any("No earnings-call transcript is indexed" in n for n in d.notes)


def test_unavailable_source_is_named_not_guessed(env, kit) -> None:
    intent = make_intent(metrics=[], qtype="narrative", unavailable=["contingent"])
    d, _, _ = draft(env, kit, intent, llm=FakeLLM(default="x"))
    assert d.status == UNAVAILABLE_SOURCE and d.text is None
    assert "contingent liabilities" in d.notes[0] and "not indexed" in d.notes[0]


# --- post-check: acceptance 5 ----------------------------------------------------------------

QUOTE_1 = "Raw material costs rose by 150 basis points in the quarter."
QUOTE_2 = "Price increases helped to offset part of the cost increase."


def rules_for(env, periods=("Q1 FY27",), bind_period=True) -> ClaimRules:
    return ClaimRules(
        periods=tuple(P(p) for p in periods),
        bind_period=bind_period,
        causal_words=tuple(env.intents.causal_words),
        management_terms=tuple(env.intents.speaker_terms["management"]),
        analyst_terms=tuple(env.intents.speaker_terms["analyst"]),
        management_roles=frozenset(env.intents.management_speaker_roles),
        min_quote_chars=env.settings.query.answer.min_quote_chars,
    )


@pytest.fixture
def checked(env, kit):
    """Evidence with F1 (14.49 %), N1 (a passage with the number 150) and N2 (results notes)."""
    kit.metric(GABRIEL, "operating_margin", "Q1 FY27", D("0.14489"))
    plan, ev, block = env.run(make_intent())
    chunks = [
        hit(QUOTE_1, cid="CHK-1", role="management"),
        hit(
            QUOTE_2,
            cid="CHK-2",
            source="results",
            section="results_notes",
            speaker=None,
            role=None,
            page=9,
        ),
    ]
    ev.chunks = chunks
    ev.chunk_aliases = {"N1": "CHK-1", "N2": "CHK-2"}
    rules = rules_for(env)

    def run(*claims, settings=None):
        parsed = parse_claims(json.loads(claims_json(*claims)))
        return postcheck(parsed, ev, block, settings or env.settings.query.answer, rules)

    return run, ev, block


def test_a_claim_without_a_citation_is_dropped(checked) -> None:
    run, _, _ = checked
    r = run(claim("Margin fell.", [], QUOTE_1), claim("Costs rose.", ["N1"], QUOTE_1))
    assert [c.bare_text for c in r.kept] == ["Costs rose."]
    assert r.dropped == [{"sentence": "Margin fell.", "reason": "no_citation"}]


def test_a_claim_citing_an_unknown_alias_is_dropped(checked) -> None:
    run, _, _ = checked
    r = run(claim("Margin fell.", ["N9"], QUOTE_1), claim("Mix helped.", ["N2"], QUOTE_2))
    assert [c.bare_text for c in r.kept] == ["Mix helped."]
    assert r.dropped[0]["reason"] == "unknown_alias: N9"


def test_a_number_must_be_in_the_quote_or_a_cited_figure(checked) -> None:
    run, _, _ = checked
    r = run(
        claim("The margin was 14.5%.", ["N1", "F1"], QUOTE_1),
        claim("The margin was 14.49%.", ["N1", "F1"], QUOTE_1),
    )
    assert [c.bare_text for c in r.kept] == ["The margin was 14.49%."]
    assert r.dropped[0]["reason"] == "number_not_in_quote: 14.5"
    assert run(claim("Costs rose by 150 basis points.", ["N1"], QUOTE_1)).kept
    assert run(claim("Costs rose by 150 basis points.", ["N2"], QUOTE_2)).dropped
    # the quote must come from a passage the claim cites
    assert run(claim("Costs rose by 150 basis points.", ["N2"], QUOTE_1)).dropped


def test_labels_years_and_the_model_writing_its_own_brackets_do_not_trip_the_check(checked) -> None:
    run, _, _ = checked
    r = run(claim("In Q1 FY27 (Apr-Jun 2026) price rises helped [N2].", ["N2"], QUOTE_2))
    assert len(r.kept) == 1 and not r.dropped and r.cited == ["N2"]
    assert r.kept[0].sentence == "In Q1 FY27 (Apr-Jun 2026) price rises helped [N2]."


def test_the_standard_refusal_is_not_a_claim_and_adds_nothing(checked) -> None:
    run, _, _ = checked
    r = run(claim(NO_EXPLANATION, [], ""))
    assert r.kept == [] and r.dropped == [] and r.explanation == ""
    assert run().kept == []


def test_check_switches_follow_settings(env, checked) -> None:
    run, _, _ = checked
    loose = env.settings.query.answer.model_copy(
        update={"require_citation_per_sentence": False, "numeric_check_enabled": False}
    )
    r = run(claim("Anything at all, 99 times.", [], ""), settings=loose)
    assert [c.bare_text for c in r.kept] == ["Anything at all, 99 times."]
    r = run(claim("Costs rose 99 percent.", ["N1"], QUOTE_1), settings=loose)
    assert len(r.kept) == 1  # the number check is switched off; the quote check is not


# --- citations: acceptance 13 ----------------------------------------------------------------


def test_citations_map_back_to_real_ids_with_source_details(checked) -> None:
    _, ev, _ = checked
    cites = {c.alias: c for c in build_citations(ev, ["F1", "N1", "N2"])}
    assert cites["F1"].kind == "metric" and cites["F1"].id == ev.metric_aliases["F1"]
    assert cites["F1"].detail["metric_id"] == "operating_margin"
    n1, n2 = cites["N1"].detail, cites["N2"].detail
    assert (n1["source_type"], n1["period_label"], n1["speaker"], n1["speaker_role"]) == (
        "transcript",
        "Q1 FY27",
        "Anand",
        "management",
    )
    assert n1["header"] == "earnings call Q1 FY27: Anand (management)"
    assert (n2["source_type"], n2["page"]) == ("results", 9)
    assert n2["header"] == "results results notes, Q1 FY27, p. 9"
    assert (
        chunk_header(
            hit("x", source="results", section="audit_opinion", page=None, speaker=None, role=None)
        )
        == "results audit opinion, Q1 FY27, XBRL text block"
    )


def test_a_multi_quarter_answer_shows_different_quarters(checked) -> None:
    _, ev, _ = checked
    ev.chunks = [hit("a", cid="C1", label="Q1 FY26"), hit("b", cid="C2", label="Q2 FY26")]
    ev.chunk_aliases = {"N1": "C1", "N2": "C2"}
    labels = [c.detail["period_label"] for c in build_citations(ev, ["N1", "N2"])]
    assert labels == ["Q1 FY26", "Q2 FY26"]
