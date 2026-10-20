"""Search (M08 acceptance 4, 5, 6, 8, 15): filters never leak, keywords are found."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml

from finrag.core.config import get_settings
from finrag.core.db import tables, transaction
from finrag.core.fiscal import label_to_period
from finrag.core.types import Period, PeriodType
from finrag.narrative.runner import run_index
from finrag.narrative.search import IndexOutOfDate, SearchRequest, search

ISIN_A, ISIN_B = "INE111A01010", "INE222B01028"
Q4 = Period(date(2026, 1, 1), date(2026, 3, 31), PeriodType.Q)
Q1 = Period(date(2026, 4, 1), date(2026, 6, 30), PeriodType.Q)


def _search(embedder, **kwargs):
    request = SearchRequest(**{"k": 50, **kwargs})
    return search(request, embedder, get_settings())


def test_acceptance_4_a_query_for_company_a_never_returns_company_b(corpus, embedder) -> None:
    hits = _search(embedder, isin=ISIN_A, query_text="gross margin mix strategic orders")
    assert hits
    with transaction() as conn:
        owner = dict(conn.execute(sa.select(tables.chunks.c.chunk_id, tables.chunks.c.isin)).all())
    assert {owner[h.chunk_id] for h in hits} == {ISIN_A}
    other = _search(embedder, isin=ISIN_B, query_text="gross margin mix strategic orders")
    assert {owner[h.chunk_id] for h in other} == {ISIN_B}
    assert not {h.chunk_id for h in hits} & {h.chunk_id for h in other}


def test_q4_fy26_and_q1_fy27_chunks_of_one_company_do_not_leak(corpus, embedder) -> None:
    both = _search(embedder, isin=ISIN_A, query_text="gross margin", source_types=["transcript"])
    assert {h.period_label for h in both} == {"Q4 FY26", "Q1 FY27"}  # same call text in both
    only_q4 = _search(
        embedder, isin=ISIN_A, query_text="gross margin", source_types=["transcript"], periods=[Q4]
    )
    only_q1 = _search(
        embedder, isin=ISIN_A, query_text="gross margin", source_types=["transcript"], periods=[Q1]
    )
    assert only_q4 and only_q1
    assert {h.period_end for h in only_q4} == {"2026-03-31"}
    assert {h.period_end for h in only_q1} == {"2026-06-30"}
    assert not {h.chunk_id for h in only_q4} & {h.chunk_id for h in only_q1}
    assert {h.filing_id for h in only_q4} == {corpus["a_q4"]}
    assert {h.filing_id for h in only_q1} == {corpus["a_q1"]}


def test_the_period_filter_is_exact_not_a_fiscal_year(corpus, embedder) -> None:
    q4_fy26 = label_to_period("Q4 FY26")
    assert _search(embedder, isin=ISIN_A, query_text="margin", periods=[q4_fy26])
    # a period type that does not match the stored one returns nothing, even for the same end date
    h1 = Period(date(2025, 10, 1), date(2026, 3, 31), PeriodType.H)
    assert _search(embedder, isin=ISIN_A, query_text="margin", periods=[h1]) == []
    assert _search(embedder, isin=ISIN_A, query_text="margin", periods=[]) == []


def test_fiscal_year_filter_is_respected(corpus, embedder) -> None:
    fy27 = _search(
        embedder,
        isin=ISIN_A,
        query_text="margin",
        fiscal_years=["FY27"],
        source_types=["transcript"],
    )
    assert fy27 and {h.fiscal_year for h in fy27} == {"FY27"}


def test_acceptance_15_source_type_and_section_filters(corpus, embedder) -> None:
    transcript = _search(embedder, isin=ISIN_A, query_text="margin", source_types=["transcript"])
    assert transcript and {h.source_type for h in transcript} == {"transcript"}
    closing = _search(
        embedder, isin=ISIN_A, query_text="guide growth", sections=["closing_remarks"]
    )
    assert closing and {h.section for h in closing} == {"closing_remarks"}
    assert all(h.speaker_role == "management" for h in closing)


def test_acceptance_5_an_exact_term_is_found_by_keyword_even_with_random_vectors(
    db: None, fx, embedder, add_filing
) -> None:
    pages = fx.transcript_pages()
    pages[2]["lines"].append(fx._line("We also launched the Zorblax accelerator in the quarter."))
    add_filing(ISIN_A, "earnings_call_transcript", "pdf", pages=pages)
    add_filing(
        ISIN_A, "earnings_call_transcript", "pdf", "Q1FY27", pages=fx.transcript_pages(), key="o"
    )
    run_index(embedder=embedder)
    hits = _search(embedder, isin=ISIN_A, query_text="Zorblax", k=6)
    # the term is in the analyst's question; its answer is indexed together with that question
    # (and may rank first), so the question chunk itself must be among the hits
    assert hits[0].scores["bm25"] > 0
    assert any("Zorblax" in h.text for h in hits)


def test_hits_carry_what_a_citation_needs(corpus, embedder) -> None:
    (hit, *_) = _search(
        embedder, isin=ISIN_A, query_text="Pictet capex funded", source_types=["transcript"]
    )
    assert hit.firm and hit.speaker and hit.speaker_role == "analyst"
    assert hit.page is not None and hit.period_label == "Q4 FY26" or hit.period_label == "Q1 FY27"
    xbrl = _search(
        embedder,
        isin=ISIN_A,
        query_text="inventory valuation",
        sections=["results_notes"],
        source_types=["results"],
        periods=[Q4],
    )
    block = next(h for h in xbrl if h.page is None)
    assert block.concept == "in-capmkt:DisclosureOfNotesOnFinancialResultsExplanatoryTextBlock"
    assert block.basis == "standalone" and block.period_label == "Q4 FY26"


def test_nothing_matching_the_filters_gives_an_empty_list(corpus, embedder) -> None:
    assert _search(embedder, isin="INE999Z01011", query_text="margin") == []
    assert _search(embedder, isin=ISIN_A, query_text="margin", sections=["mdna"]) == []


def test_chunks_missing_from_the_index_are_an_error_not_a_silent_gap(
    corpus, fx, embedder, add_filing
) -> None:
    add_filing(ISIN_A, "earnings_call_transcript", "pdf", pages=fx.transcript_pages(), key="late")
    from finrag.narrative import store

    with transaction() as conn:  # chunks written but the indexes were not refreshed
        filing = conn.execute(
            sa.select(tables.filings).order_by(tables.filings.c.created_at.desc())
        ).first()
        from finrag.narrative.models import DraftChunk

        store.replace_for_filing(
            conn, filing, "transcript", "FY26", [DraftChunk("qa", "new text", 2, 1)]
        )
    with pytest.raises(IndexOutOfDate):
        _search(embedder, isin=ISIN_A, query_text="new text")


def test_acceptance_8_retrieval_eval_recall(corpus, embedder) -> None:
    cases = yaml.safe_load((Path(__file__).parent / "retrieval_cases.yaml").read_text("utf-8"))
    found = 0
    for case in cases["cases"]:
        request = SearchRequest(
            isin=case["isin"],
            query_text=case["question"],
            sections=case.get("sections"),
            source_types=case.get("source_types"),
            periods=[label_to_period(p) for p in case["periods"]] if "periods" in case else None,
            k=cases["k"],
        )
        hits = search(request, embedder, get_settings())
        found += any(case["expect_contains"] in h.text for h in hits)
    recall = found / len(cases["cases"])
    print(f"recall@{cases['k']} = {recall:.2f} on {len(cases['cases'])} cases")
    assert recall >= cases["min_recall"]


@pytest.mark.slow
def test_acceptance_6_paraphrase_with_the_real_model(db: None, fx, add_filing, monkeypatch) -> None:
    """The configured model finds a paraphrase that shares no keyword (needs the cached model)."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # no network in tests: the model must be cached
    from finrag.narrative.embedder import SentenceTransformerEmbedder

    try:
        embedder = SentenceTransformerEmbedder(get_settings().narrative)
        embedder.embed(["warm up"])
    except Exception as exc:  # model not cached on this machine
        pytest.skip(f"embedding model not available offline: {exc}")
    add_filing(ISIN_A, "earnings_call_transcript", "pdf", pages=fx.transcript_pages())
    run_index(embedder=embedder)
    hits = _search(
        embedder, isin=ISIN_A, query_text="how will the business pay for new equipment", k=3
    )
    assert any("capex" in h.text.lower() for h in hits)


def test_a_speaker_role_filter_keeps_only_those_turns(corpus, embedder) -> None:
    """Added with M09: 'what did management say' must not return an analyst's turn."""
    everyone = _search(embedder, isin=ISIN_A, query_text="margin", source_types=["transcript"])
    roles = {h.speaker_role for h in everyone}
    assert "management" in roles and len(roles) > 1, roles
    for wanted in roles:
        only = _search(
            embedder,
            isin=ISIN_A,
            query_text="margin",
            source_types=["transcript"],
            speaker_roles=[wanted],
        )
        assert only and {h.speaker_role for h in only} == {wanted}
    assert _search(embedder, isin=ISIN_A, query_text="margin", speaker_roles=["nobody"]) == []
