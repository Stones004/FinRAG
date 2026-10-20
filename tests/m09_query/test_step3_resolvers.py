"""Step 3: entity, metric, period and question-type resolvers."""

from __future__ import annotations

from datetime import date

import pytest

from finrag.core.fiscal import label_to_period
from finrag.core.types import Period, PeriodType
from finrag.metrics.registry import load_metrics
from finrag.query.availability import StubAvailability, duration_for_instant
from finrag.query.entity_resolver import load_entities, resolve_entity
from finrag.query.metric_resolver import MetricResolver
from finrag.query.models import (
    AMBIGUOUS_METRIC,
    MISSING_METRIC,
    MULTI_ENTITY,
    NO_ENTITY,
    UNKNOWN_COMPANY,
    UNKNOWN_METRIC,
)
from finrag.query.period_resolver import (
    calendar_last_quarter,
    describe_period,
    parse_periods,
    resolve_periods,
)
from finrag.query.qtype import classify_question
from finrag.query.text import Working, normalize

from .conftest import GABRIEL, HDFC, NETWEB, TODAY

pytestmark = pytest.mark.usefixtures("seeded")


def P(label: str) -> Period:
    return label_to_period(label)


# --- text ------------------------------------------------------------------------------------


def test_normalize(intents) -> None:
    assert normalize("What's HDFC's NII for FY'26?", intents) == " what is hdfc nii for fy'26 "
    assert normalize("D/E & D&A, ok.", intents) == " d/e & d&a ok "


# --- entity ----------------------------------------------------------------------------------


@pytest.fixture
def entities():
    return load_entities()


def entity(q: str, intents, settings, entities):
    work = Working(q, intents)
    return resolve_entity(work, entities, settings, set(intents.question_starters)), work


@pytest.mark.parametrize(
    "q,isin",
    [
        ("what was Gabriel's margin", GABRIEL),
        ("hdfc nii", HDFC),
        ("HDFC Bank's cost to income", HDFC),
        ("hdfcbank gnpa", HDFC),
        ("Netweb Technologies India revenue", NETWEB),
        ("netweb", NETWEB),
    ],
)
def test_exact_aliases_give_confidence_one(q, isin, intents, settings, entities) -> None:
    result, work = entity(q, intents, settings, entities)
    assert (result.isin, result.confidence, result.problem) == (isin, 1.0, None)


def test_entity_words_are_consumed(intents, settings, entities) -> None:
    result, work = entity("hdfc bank nii", intents, settings, entities)
    assert "hdfc" not in work.work and "nii" in work.work


def test_fuzzy_entity_confidence_is_score(intents, settings, entities) -> None:
    result, _ = entity("netwab revnue growth", intents, settings, entities)
    assert result.isin == NETWEB and result.confidence == pytest.approx(0.833, abs=0.01)
    result, _ = entity("gabrel margin", intents, settings, entities)
    assert result.isin == GABRIEL and 0.9 < result.confidence < 1.0


def test_two_companies_is_multi_entity(intents, settings, entities) -> None:
    result, _ = entity("compare Gabriel and Netweb ROE", intents, settings, entities)
    assert result.problem == MULTI_ENTITY and result.isin is None
    assert {c.id for c in result.candidates} == {GABRIEL, NETWEB}


def test_no_company_is_no_entity_with_every_company_offered(intents, settings, entities) -> None:
    result, _ = entity("what's the operating margin?", intents, settings, entities)
    assert result.problem == NO_ENTITY and len(result.candidates) == 3


def test_unknown_capitalised_name(intents, settings, entities) -> None:
    result, _ = entity("Reliance revenue FY26", intents, settings, entities)
    assert result.problem == UNKNOWN_COMPANY and result.unknown_names == ["Reliance"]
    # a question opener or an abbreviation is not a company name
    result, _ = entity("Show me ROE for last year", intents, settings, entities)
    assert result.problem == NO_ENTITY


def test_single_tracked_company_is_the_default(intents, settings, entities) -> None:
    result, _ = entity("operating margin", intents, settings, entities[:1])
    assert (result.isin, result.defaulted, result.confidence) == (entities[0].isin, True, 1.0)


# --- metric ----------------------------------------------------------------------------------


@pytest.fixture
def metrics(intents, settings):
    return MetricResolver(load_metrics(), intents, settings)


def metric(q, metrics, intents, sector="general"):
    work = Working(q, intents)
    return metrics.resolve(work, sector), work


@pytest.mark.parametrize(
    "q,ids",
    [
        ("gabriel opm", ["operating_margin"]),
        ("operating margin change", ["operating_margin_change_yoy"]),
        ("ebitda margin", ["ebitda_margin"]),
        ("ebitda", ["ebitda"]),
        ("pat growth", ["pat_growth_yoy"]),
        ("pat", ["profit_after_tax"]),
        ("what was revenue", ["revenue"]),
        ("cfo", ["cfo"]),
        ("operating cash flow", ["cfo"]),
        ("fcf and dso", ["fcf", "dso"]),
        ("asset quality", ["gross_npa_ratio"]),
        ("gnpa", ["gross_npa_ratio"]),
        ("gross npa", ["gross_npa"]),
        ("gross npa ratio", ["gross_npa_ratio"]),
        ("cd ratio", ["credit_deposit_ratio"]),
        ("d/e", ["debt_to_equity"]),
        ("current ratio", ["current_ratio"]),
    ],
)
def test_longest_synonym_first(q, ids, metrics, intents) -> None:
    result, _ = metric(q, metrics, intents)
    assert result.ids == ids and result.confidence == 1.0 and result.problem is None


def test_roe_is_chosen_by_sector(metrics, intents) -> None:
    assert metric("roe", metrics, intents, "general")[0].ids == ["roe"]
    assert metric("roe", metrics, intents, "bank")[0].ids == ["roe_bank"]


def test_metric_for_the_wrong_sector_is_still_returned(metrics, intents) -> None:
    # HDFC operating margin: the metric is named; retrieval reports not_applicable
    assert metric("operating margin", metrics, intents, "bank")[0].ids == ["operating_margin"]


@pytest.mark.parametrize(
    "q,metric_id,low,high",
    [
        ("oparating margin", "operating_margin", 0.9, 1.0),
        ("cost to incom", "cost_to_income", 0.9, 1.0),
        ("revnue growth", "revenue_growth_yoy", 0.9, 1.0),
        ("ebidta", "ebitda", 0.8, 0.85),
        ("advnces growth", "advances_growth_yoy", 0.9, 1.0),
        ("free cashflow", "fcf", 0.9, 1.0),
    ],
)
def test_typos_are_fuzzy_with_score_as_confidence(
    q, metric_id, low, high, metrics, intents
) -> None:
    result, _ = metric(q, metrics, intents)
    assert result.ids == [metric_id] and low <= result.confidence <= high


def test_ambiguous_generic_word_lists_the_candidates(metrics, intents) -> None:
    result, _ = metric("margin", metrics, intents)
    assert result.problem == AMBIGUOUS_METRIC
    assert {"operating_margin", "ebitda_margin", "net_margin"} <= {c.id for c in result.candidates}


def test_unknown_metric_lists_nearest(metrics, intents) -> None:
    result, _ = metric("sharpe ratio", metrics, intents)
    assert result.problem == UNKNOWN_METRIC and result.unknown_words == ["sharpe"]
    assert result.candidates and len(result.candidates) <= 8


def test_nothing_asked_is_missing_metric(metrics, intents) -> None:
    assert metric("how is it doing", metrics, intents)[0].problem == MISSING_METRIC


def test_phrase_tables_have_no_shared_metric_and_concept_phrase(metrics) -> None:
    for phrase, entries in metrics._phrases.items():
        kinds = [k for k, _ in entries]
        assert not ("metric" in kinds and "concept" in kinds), phrase


# --- period ----------------------------------------------------------------------------------


def parse(q, intents):
    work = Working(q, intents)
    return parse_periods(work, intents), work


@pytest.mark.parametrize(
    "q,labels",
    [
        ("q1 fy27", ["Q1 FY27"]),
        ("Q1FY27", ["Q1 FY27"]),
        ("q4 fy'26", ["Q4 FY26"]),
        ("h1 fy26", ["H1 FY26"]),
        ("9m fy26", ["9M FY26"]),
        ("fy2026", ["FY26"]),
        ("fy 26 vs fy25", ["FY26", "FY25"]),
        ("q1 fy 2027 and q1 fy26", ["Q1 FY27", "Q1 FY26"]),
    ],
)
def test_explicit_periods(q, labels, intents, settings, avail) -> None:
    p, _ = parse(q, intents)
    out = resolve_periods(p, GABRIEL, [], [], avail, settings, TODAY)
    assert [x.label for x in p.mentions] == labels
    assert out.periods == [P(x) for x in labels] and out.confidence == 1.0 and not out.defaulted


def test_a_quarter_label_is_not_also_read_as_a_year(intents, settings, avail) -> None:
    p, work = parse("operating margin q1 fy27", intents)
    assert [m.kind for m in p.mentions] == ["quarter"] and "fy27" not in work.work


def test_two_named_periods_are_a_periods_comparison(intents, settings, avail) -> None:
    p, _ = parse("fy26 vs fy25", intents)
    out = resolve_periods(p, GABRIEL, [], [], avail, settings, TODAY)
    assert out.comparison == "periods"


def test_comparison_words_are_removed_before_periods(intents, settings, avail) -> None:
    p, work = parse("revenue growth vs last quarter", intents)
    out = resolve_periods(p, GABRIEL, ["revenue_growth_yoy"], [], avail, settings, TODAY)
    assert out.comparison == "sequential" and p.mentions == []
    assert out.defaulted and out.periods == [P("Q1 FY27")]


@pytest.mark.parametrize(
    "q,comparison",
    [
        ("operating margin yoy", "yoy"),
        ("pat qoq", "sequential"),
        ("consolidated vs standalone margin", "basis"),
        ("margin trend", "trend"),
    ],
)
def test_comparison_kinds(q, comparison, intents, settings, avail) -> None:
    p, _ = parse(q, intents)
    out = resolve_periods(p, GABRIEL, [], [], avail, settings, TODAY)
    assert out.comparison == comparison


def test_last_n_years_and_last_year(intents, settings, avail) -> None:
    p, _ = parse("over the last 3 years", intents)
    out = resolve_periods(p, GABRIEL, [], [], avail, settings, TODAY)
    assert out.periods == [P("FY24"), P("FY25"), P("FY26")] and out.comparison == "trend"
    p, _ = parse("last year", intents)
    out = resolve_periods(p, GABRIEL, [], [], avail, settings, TODAY)
    assert out.periods == [P("FY26")] and out.confidence == 1.0


def test_last_n_years_is_capped(intents, settings, avail) -> None:
    p, _ = parse("last 50 years", intents)
    out = resolve_periods(p, GABRIEL, [], [], avail, settings, TODAY)
    assert len(out.periods) == settings.intent.max_last_n_years


def test_calendar_last_quarter() -> None:
    assert calendar_last_quarter(TODAY) == P("Q2 FY27")
    assert calendar_last_quarter(date(2026, 4, 2)) == P("Q4 FY26")


def test_last_quarter_not_filed_shows_latest_and_says_so(intents, settings, avail) -> None:
    p, _ = parse("last quarter", intents)
    out = resolve_periods(p, GABRIEL, ["operating_margin"], [], avail, settings, TODAY)
    assert out.periods == [P("Q1 FY27")]
    assert out.confidence == settings.intent.unfiled_period_confidence
    assert out.confidence < settings.intent.auto_proceed_threshold
    assert out.notes == [
        "Q2 FY27 (Jul–Sep 2026) is not filed; showing Q1 FY27 (Apr–Jun 2026), the latest filed"
    ]


def test_last_quarter_filed_is_used_as_is(intents, settings) -> None:
    filed = StubAvailability([P("Q2 FY27"), P("Q1 FY27")])
    p, _ = parse("last quarter", intents)
    out = resolve_periods(p, GABRIEL, [], [], filed, settings, TODAY)
    assert out.periods == [P("Q2 FY27")] and out.confidence == 1.0 and not out.notes


def test_last_quarter_when_the_latest_data_is_a_year_walks_back_to_a_quarter(
    intents, settings
) -> None:
    only_years = StubAvailability([P("FY26"), P("Q4 FY26"), P("Q3 FY26")], {"fcf": [P("FY26")]})
    p, _ = parse("last quarter", intents)
    out = resolve_periods(p, GABRIEL, ["fcf"], [], only_years, settings, TODAY)
    assert out.periods == [P("Q4 FY26")] and out.notes


def test_no_period_defaults_to_latest_with_data(intents, settings, avail) -> None:
    p, _ = parse("gross npa", intents)
    out = resolve_periods(p, HDFC, [], [], avail, settings, TODAY)
    assert out.defaulted and out.periods == [P("Q1 FY27")]
    assert out.confidence == settings.intent.default_period_confidence
    p, _ = parse("gnpa latest", intents)
    assert p.latest


def test_no_data_at_all(intents, settings) -> None:
    p, _ = parse("gross npa", intents)
    out = resolve_periods(p, HDFC, [], [], StubAvailability([]), settings, TODAY)
    assert out.periods == [] and out.notes


def test_describe_period() -> None:
    assert describe_period(P("Q1 FY26")) == "Q1 FY26 (Apr–Jun 2025)"
    assert describe_period(P("FY26")) == "FY26 (Apr 2025–Mar 2026)"


def test_instant_dates_map_to_fiscal_periods() -> None:
    assert duration_for_instant(date(2026, 3, 31)) == P("FY26")
    assert duration_for_instant(date(2025, 9, 30)) == P("H1 FY26")
    assert duration_for_instant(date(2025, 8, 15)) is None
    assert P("FY26").type is PeriodType.FY


# --- question type ---------------------------------------------------------------------------


def kind(q, intents, has_metric=True, signal=False):
    return classify_question(Working(q, intents), intents, has_metric, signal)


@pytest.mark.parametrize(
    "q,qtype",
    [
        ("what was the margin", "lookup"),
        ("what's the margin", "lookup"),
        ("why did margin fall", "explanation"),
        ("what was the reason for the fall", "explanation"),  # explanation beats lookup
        ("what caused the change in cfo", "explanation"),  # explanation beats comparison
        ("what did management say about margins", "narrative"),
        ("guidance for fy27", "narrative"),
        ("compare fy26 and fy25", "comparison"),
        ("margin trend over the last 3 years", "comparison"),
        ("gabriel opm", "lookup"),  # a metric and no cue: the figure only
        ("hdfc gnpa latest", "lookup"),
        ("netweb margin driver", "explanation"),
        ("why is hdfc gnpa up", "explanation"),
        ("what is the cause of the margin fall", "explanation"),
        ("how come hdfc nii fell", "explanation"),
        ("netweb commentary on revenue", "narrative"),
        ("what was said about gnpa", "narrative"),
        ("did they mention capex", "narrative"),
    ],
)
def test_question_type_order(q, qtype, intents) -> None:
    assert kind(q, intents).question_type == qtype


def test_comparison_signal_counts_as_a_comparison_keyword(intents) -> None:
    assert kind("opm q1 fy27", intents, signal=True).question_type == "comparison"


def test_no_keyword_no_metric(intents) -> None:
    assert kind("how is hdfc doing", intents, has_metric=False).question_type is None


def test_section_hints_and_unavailable_sources(intents) -> None:
    k = kind("what are the contingent liabilities", intents, has_metric=False)
    assert k.question_type == "narrative" and k.unavailable == ["contingent"] and not k.sections
    k = kind("what did the auditor say", intents, has_metric=False)
    assert k.sections == ["auditor_report"] and k.question_type == "narrative"
    k = kind("is there any key audit matter", intents, has_metric=False)
    assert k.unavailable == ["key_audit_matters"] and k.question_type == "narrative"
    assert kind("hdfc asset quality", intents).sections == ["press_release"]
