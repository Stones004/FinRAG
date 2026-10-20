"""Step 5: planner (source order, quarters, no annual reports), MMR, retriever, evidence aliases."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from finrag.core.config import get_settings
from finrag.core.fiscal import label_to_period
from finrag.metrics.registry import load_metrics
from finrag.narrative.search import SearchHit
from finrag.normalize.taxonomy import load_taxonomy
from finrag.query.intents import load_intents
from finrag.query.mmr import select_mmr
from finrag.query.models import Intent
from finrag.query.planner import Planner
from finrag.query.retriever import Retriever

from .conftest import GABRIEL, HDFC

pytestmark = pytest.mark.usefixtures("seeded")


def P(label):
    return label_to_period(label)


def intent(
    metrics=("operating_margin",),
    periods=("Q1 FY27",),
    qtype="explanation",
    basis="consolidated",
    comparison=None,
    sections=(),
    unavailable=(),
    isin=GABRIEL,
) -> Intent:
    return Intent(
        question="why did operating margin change",
        entity_isin=isin,
        entity_name="X",
        metrics=list(metrics),
        periods=[P(p) for p in periods],
        comparison=comparison,
        question_type=qtype,
        basis=basis,
        sections=list(sections),
        confidence=1.0,
        decision="proceed",
        unavailable_sources=list(unavailable),
    )


@pytest.fixture
def planner() -> Planner:
    taxonomy = load_taxonomy()
    return Planner(load_metrics(taxonomy), taxonomy, load_intents(taxonomy), get_settings())


def groups(plan):
    return [(g["order"], g["source_type"], g["periods"], g["sections"]) for g in plan.search_groups]


DEFAULT_SECTIONS = ["results_notes", "segment_notes", "press_release"]  # no auditor text


# --- planner: acceptance 11 ------------------------------------------------------------------


def test_quarterly_why_is_transcript_first_then_that_quarters_results_text(planner) -> None:
    plan = planner.plan(intent(periods=["Q1 FY27"]))
    assert groups(plan) == [
        (1, "transcript", ["Q1 FY27"], None),
        (2, "results", ["Q1 FY27"], DEFAULT_SECTIONS),
    ]
    assert [s.source_types for s in plan.searches] == [["transcript"], ["results"]]
    assert plan.searches[0].periods == [P("Q1 FY27")]
    assert plan.searches[0].k == get_settings().query.retrieval.k_candidates


@pytest.mark.parametrize(
    "period,calls,results",
    [
        ("H1 FY26", ["Q1 FY26", "Q2 FY26"], ["Q2 FY26"]),
        ("9M FY26", ["Q1 FY26", "Q2 FY26", "Q3 FY26"], ["Q3 FY26"]),
        ("FY26", ["Q1 FY26", "Q2 FY26", "Q3 FY26", "Q4 FY26"], ["Q4 FY26"]),
    ],
)
def test_cumulative_periods_search_every_quarter_inside_them(planner, period, calls, results):
    plan = planner.plan(intent(metrics=["revenue"], periods=[period]))
    assert groups(plan)[0] == (1, "transcript", calls, None)
    assert groups(plan)[1] == (2, "results", results, DEFAULT_SECTIONS)
    assert plan.searches[0].periods == [P(x) for x in calls]
    assert plan.searches[1].periods == [P(x) for x in results]


def test_no_plan_ever_searches_an_annual_report(planner) -> None:
    for period in ("Q1 FY27", "H1 FY26", "9M FY26", "FY26"):
        for sections in ((), ("auditor_report",), ("press_release",)):
            plan = planner.plan(intent(periods=[period], sections=sections))
            for request in plan.searches:
                assert request.source_types in (["transcript"], ["results"])
                assert "annual_report" not in (request.sections or [])


def test_an_explicit_results_section_goes_straight_to_it(planner) -> None:
    plan = planner.plan(intent(metrics=[], qtype="narrative", sections=["auditor_report"]))
    assert groups(plan) == [(1, "results", ["Q1 FY27"], ["auditor_report"])]


def test_a_topical_hint_adds_a_section_but_keeps_the_transcripts(planner) -> None:
    plan = planner.plan(
        intent(
            metrics=["gross_npa_ratio"], qtype="narrative", sections=["press_release"], isin=HDFC
        )
    )
    assert [g[1] for g in groups(plan)] == ["transcript", "results"]
    assert groups(plan)[1][3].count("press_release") == 1


def test_sources_not_indexed_are_named_and_not_searched(planner) -> None:
    plan = planner.plan(intent(metrics=[], qtype="narrative", unavailable=["contingent"]))
    assert plan.searches == [] and plan.unavailable_sources == ["contingent"]


def test_lookup_and_comparison_do_no_narrative_search(planner) -> None:
    assert planner.plan(intent(qtype="lookup")).searches == []
    assert planner.plan(intent(qtype="comparison")).searches == []


def test_plan_json_shows_the_order_and_quarters(planner) -> None:
    js = planner.plan(intent(periods=["H1 FY26"])).to_json()
    assert js["searches"][0] == {
        "order": 1,
        "source_type": "transcript",
        "periods": ["Q1 FY26", "Q2 FY26"],
        "sections": None,
    }


# --- planner: figures ------------------------------------------------------------------------


def roles(plan):
    return [(r.metric, period_label(r.period), r.basis, r.role) for r in plan.requests]


def period_label(p):
    from finrag.core.fiscal import period_to_label

    return period_to_label(p)


def test_main_figure_and_the_other_basis_offered(planner) -> None:
    plan = planner.plan(intent(qtype="lookup"))
    assert roles(plan) == [
        ("operating_margin", "Q1 FY27", "consolidated", "main"),
        ("operating_margin", "Q1 FY27", "standalone", "other_basis"),
    ]


def test_basis_comparison_requests_both_bases_as_main(planner) -> None:
    plan = planner.plan(intent(qtype="comparison", basis=None, comparison="basis"))
    assert roles(plan) == [
        ("operating_margin", "Q1 FY27", "consolidated", "main"),
        ("operating_margin", "Q1 FY27", "standalone", "main"),
    ]


def test_yoy_and_sequential_add_the_earlier_period(planner) -> None:
    yoy = planner.plan(intent(qtype="comparison", comparison="yoy"))
    assert ("operating_margin", "Q1 FY26", "consolidated", "prior_year") in roles(yoy)
    seq = planner.plan(intent(qtype="comparison", comparison="sequential"))
    assert ("operating_margin", "Q4 FY26", "consolidated", "previous_period") in roles(seq)


def test_companions_are_added_for_why_questions_only(planner) -> None:
    why = planner.plan(intent(metrics=["profit_after_tax"], qtype="explanation"))
    companions = [r.metric for r in why.requests if r.role == "companion"]
    assert companions == [
        "profit_continuing_ops",
        "share_of_associates_jv",
        "associates_share_of_pat",
    ]
    look = planner.plan(intent(metrics=["profit_after_tax"], qtype="lookup"))
    assert not [r for r in look.requests if r.role == "companion"]


def test_companions_naming_unknown_metrics_fail_at_load() -> None:
    from finrag.core.config import ConfigError

    taxonomy = load_taxonomy()
    bad = load_intents(taxonomy).model_copy(update={"companions": {"nope": ["revenue"]}})
    with pytest.raises(ConfigError, match="nope"):
        Planner(load_metrics(taxonomy), taxonomy, bad, get_settings())


# --- MMR -------------------------------------------------------------------------------------


class VectorEmbedder:
    """Embeds a text as the vector registered for it."""

    name = "vec"

    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors

    def embed(self, texts):
        out = np.array([self.vectors[t] for t in texts], dtype=np.float32)
        return out / np.linalg.norm(out, axis=1, keepdims=True)

    def embed_query(self, text):
        return self.embed([text])[0]


def hit(text: str, cid: str | None = None, source="transcript", label="Q1 FY27") -> SearchHit:
    return SearchHit(
        chunk_id=cid or f"CHK-{text}",
        filing_id="FIL-x",
        section="qa",
        source_type=source,
        speaker="A",
        speaker_role="analyst",
        firm="F",
        page=1,
        end_page=1,
        concept=None,
        basis=None,
        fiscal_year="FY27",
        period_start=None,
        period_end=None,
        period_type="Q",
        period_label=label,
        text=text,
    )


def test_mmr_drops_near_duplicates_for_a_different_passage() -> None:
    # four analysts ask the same thing (near-identical vectors); one passage answers differently
    emb = VectorEmbedder(
        {
            "q": [1, 0, 0],
            "ask1": [1, 0.01, 0],
            "ask2": [1, 0.02, 0],
            "ask3": [1, 0.03, 0],
            "ask4": [1, 0.04, 0],
            "answer": [0.7, 0.7, 0],
        }
    )
    hits = [hit(t) for t in ("ask1", "ask2", "ask3", "ask4", "answer")]
    chosen = select_mmr(hits, "q", emb, 2, 0.3)
    assert [h.text for h in chosen] == ["ask1", "answer"]
    # pure relevance (lambda 1) keeps the duplicates
    assert [h.text for h in select_mmr(hits, "q", emb, 2, 1.0)] == ["ask1", "ask2"]


def test_mmr_returns_all_when_few_and_never_embeds() -> None:
    emb = VectorEmbedder({})
    hits = [hit("a"), hit("b")]
    assert select_mmr(hits, "q", emb, 5, 0.7) == hits  # would raise KeyError if it embedded


# --- retriever: figures ----------------------------------------------------------------------


@pytest.fixture
def retriever():
    taxonomy = load_taxonomy()
    return Retriever(None, get_settings(), load_metrics(taxonomy), taxonomy)


def retrieve(planner, retriever, **kwargs):
    return retriever.retrieve(planner.plan(intent(**kwargs)))


def test_metric_value_gets_an_alias_and_other_basis_is_dropped_when_empty(
    planner, retriever, kit
) -> None:
    kit.metric(GABRIEL, "operating_margin", "Q1 FY27", Decimal("0.1449"))
    ev = retrieve(planner, retriever, qtype="lookup")
    assert [e.alias for e in ev.entries] == ["F1"]
    assert ev.metric_aliases["F1"] == ev.metric_values[0].metric_value_id
    assert ev.missing == []
    kit.metric(GABRIEL, "operating_margin", "Q1 FY27", Decimal("0.15"), basis="standalone")
    ev = retrieve(planner, retriever, qtype="lookup")
    assert [(e.request.role, e.alias) for e in ev.entries] == [
        ("main", "F1"),
        ("other_basis", "F2"),
    ]


def test_not_computed_metric_keeps_its_reason(planner, retriever, kit) -> None:
    kit.metric(
        HDFC,
        "operating_margin",
        "Q1 FY27",
        None,
        reason="Not applicable to bank companies; defined for general.",
        details={"code": "not_applicable"},
    )
    ev = retrieve(planner, retriever, qtype="lookup", isin=HDFC)
    entry = ev.entries[0]
    assert entry.metric_value.reason.startswith("Not applicable") and entry.alias == "F1"


def test_missing_gaps_are_reported_with_the_reason(planner, retriever) -> None:
    ev = retrieve(planner, retriever, qtype="lookup")
    assert ev.entries[0].metric_value is None and ev.entries[0].alias is None
    assert ev.missing == ["Operating margin Q1 FY27 (consolidated): never computed for this period"]
    ev = retrieve(planner, retriever, metrics=["fcf"], periods=["Q1 FY27"], qtype="lookup")
    assert "not defined for a Q period (defined for H, FY)" in ev.missing[0]


def test_balance_sheet_metrics_are_read_at_the_balance_sheet_date(planner, retriever, kit) -> None:
    kit.metric(GABRIEL, "current_ratio", "FY26", Decimal("1.6636"), instant_end="2026-03-31")
    ev = retrieve(planner, retriever, metrics=["current_ratio"], periods=["FY26"], qtype="lookup")
    assert ev.entries[0].alias == "F1"
    ev = retrieve(
        planner, retriever, metrics=["current_ratio"], periods=["Q1 FY27"], qtype="lookup"
    )
    assert "no balance sheet at 2026-06-30" in ev.missing[0]


def test_concept_lookup_carries_the_restatement_and_both_filings(planner, retriever, kit) -> None:
    original = kit.filing(GABRIEL, "FY26", key="orig", filed_on="2026-05-27")
    revision = kit.filing(GABRIEL, "FY26", key="rev", revision=1, filed_on="2026-06-29")
    old = kit.fact(GABRIEL, "cfo", "FY26", 4435590000, filing_id=original, filed_on="2026-05-27")
    new = kit.fact(GABRIEL, "cfo", "FY26", 3452740000, filing_id=revision, filed_on="2026-06-29")
    kit.restatement(GABRIEL, "cfo", "FY26", "consolidated", old, new, 982850000)
    ev = retrieve(planner, retriever, metrics=["cfo"], periods=["FY26"], qtype="lookup")
    entry = ev.entries[0]
    assert entry.fact.value == Decimal(3452740000) and entry.alias == "F1"
    (r,) = entry.restatements
    assert (r["old_value"], r["new_value"]) == (Decimal(4435590000), Decimal(3452740000))
    assert (r["old_filing"], r["new_filing"]) == (original, revision)
    assert r["flagged"] is True and r["new_submission"] == "revision"
    assert ev.fact_aliases["F1"] == new


def test_companion_figures_are_kept_only_when_not_zero(planner, retriever, kit) -> None:
    kit.fact(GABRIEL, "profit_after_tax", "Q1 FY27", 1081260000)
    kit.fact(GABRIEL, "profit_continuing_ops", "Q1 FY27", 653830000)
    kit.fact(GABRIEL, "share_of_associates_jv", "Q1 FY27", 0)  # tagged 0: dropped
    kit.metric(GABRIEL, "associates_share_of_pat", "Q1 FY27", Decimal("0.3953"))
    ev = retrieve(planner, retriever, metrics=["profit_after_tax"], qtype="explanation")
    shown = [(e.request.metric, e.request.role, e.alias) for e in ev.entries]
    assert shown == [
        ("profit_after_tax", "main", "F1"),
        ("profit_continuing_ops", "companion", "F2"),
        ("associates_share_of_pat", "companion", "F3"),
    ]
    assert ev.missing == []  # a missing companion is not a gap in the answer


def test_figures_are_capped_by_max_facts_in_prompt(planner, retriever, kit, isolated_repo) -> None:
    import yaml

    from finrag.core.config import load_settings

    path = isolated_repo / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["query"]["answer"]["max_facts_in_prompt"] = 2
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    small = load_settings(path, root=isolated_repo)
    for label in ("Q1 FY27", "Q4 FY26", "Q3 FY26"):
        kit.metric(GABRIEL, "operating_margin", label, Decimal("0.1"))
    r = Retriever(None, small, retriever.registry, retriever.taxonomy)
    ev = r.retrieve(
        planner.plan(
            intent(periods=["Q1 FY27", "Q4 FY26", "Q3 FY26"], qtype="lookup", basis="consolidated")
        )
    )
    assert [e.alias for e in ev.entries if e.alias] == ["F1", "F2"]
    assert any("only the first 2" in w for w in ev.warnings)


# --- retriever: narrative --------------------------------------------------------------------


def fake_search(monkeypatch, by_source: dict[str, list[SearchHit]], calls: list):
    def _search(request, embedder, settings=None, engine=None):
        calls.append(request)
        return list(by_source.get(request.source_types[0], []))

    monkeypatch.setattr("finrag.query.retriever.search", _search)


class Plain:
    """An embedder whose vectors make every text equally relevant and dissimilar."""

    name = "plain"

    def embed(self, texts):
        eye = np.eye(max(len(texts), 1), 64, dtype=np.float32)
        return eye[: len(texts)]

    def embed_query(self, text):
        return np.ones(64, dtype=np.float32) / 8


def narrative_retriever():
    taxonomy = load_taxonomy()
    return Retriever(Plain(), get_settings(), load_metrics(taxonomy), taxonomy)


def test_transcript_chunks_fill_the_prompt_first_results_text_only_fills_the_rest(
    planner, monkeypatch
) -> None:
    calls: list = []
    transcript = [hit(f"t{i}", source="transcript") for i in range(3)]
    results = [hit(f"r{i}", source="results") for i in range(6)]
    fake_search(monkeypatch, {"transcript": transcript, "results": results}, calls)
    ev = narrative_retriever().retrieve(planner.plan(intent()))
    slots = get_settings().query.answer.max_chunks_in_prompt
    assert [c.source_type for c in ev.chunks] == ["transcript"] * 3 + ["results"] * (slots - 3)
    assert list(ev.chunk_aliases) == [f"N{i}" for i in range(1, slots + 1)]
    assert [c.source_types for c in calls] == [["transcript"], ["results"]]


def test_enough_transcript_chunks_leave_no_slot_for_results_text(planner, monkeypatch) -> None:
    calls: list = []
    slots = get_settings().query.answer.max_chunks_in_prompt
    transcript = [hit(f"t{i}", source="transcript") for i in range(slots + 3)]
    fake_search(
        monkeypatch, {"transcript": transcript, "results": [hit("r", source="results")]}, calls
    )
    ev = narrative_retriever().retrieve(planner.plan(intent()))
    assert {c.source_type for c in ev.chunks} == {"transcript"} and len(ev.chunks) == slots
    assert ev.transcripts_missing is False


def test_no_transcript_indexed_uses_the_results_text_and_says_so(planner, monkeypatch) -> None:
    calls: list = []
    fake_search(
        monkeypatch, {"results": [hit("r1", source="results"), hit("r2", source="results")]}, calls
    )
    ev = narrative_retriever().retrieve(planner.plan(intent()))
    assert ev.transcripts_missing is True
    assert [c.source_type for c in ev.chunks] == ["results", "results"]


def test_an_out_of_date_index_is_a_warning_not_a_crash(planner, monkeypatch) -> None:
    from finrag.narrative.search import IndexOutOfDate

    def boom(*a, **k):
        raise IndexOutOfDate("run `finrag narrative index`")

    monkeypatch.setattr("finrag.query.retriever.search", boom)
    ev = narrative_retriever().retrieve(planner.plan(intent()))
    assert ev.chunks == [] and any("out of date" in w for w in ev.warnings)


def test_other_basis_that_falls_back_to_the_main_basis_is_not_offered(
    planner, retriever, kit
) -> None:
    """HDFC's consolidated NPA is the standalone figure: not a second basis."""
    kit.metric(
        HDFC,
        "gross_npa_ratio",
        "Q1 FY27",
        Decimal("0.0117"),
        basis="standalone",
        details={"code": "computed", "basis_used": "standalone"},
    )
    kit.metric(
        HDFC,
        "gross_npa_ratio",
        "Q1 FY27",
        Decimal("0.0117"),
        basis="consolidated",
        details={
            "code": "computed",
            "basis_requested": "consolidated",
            "basis_used": "standalone",
            "basis_fallback": "standalone",
        },
    )
    ev = retrieve(
        planner,
        retriever,
        metrics=["gross_npa_ratio"],
        qtype="lookup",
        isin=HDFC,
        basis="standalone",
    )
    assert [(e.request.role, e.request.basis) for e in ev.entries] == [("main", "standalone")]
