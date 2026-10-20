"""Indexing runs (M08 acceptance 2, 7, 10, 11, 14, 15): chunks, summaries, supersession."""

from __future__ import annotations

import json
from types import ModuleType

import sqlalchemy as sa

from finrag.core.config import get_settings
from finrag.core.db import tables, transaction
from finrag.narrative.runner import SUMMARY_FILE, run_index

ISIN_A = "INE111A01010"


def _rows(corpus: dict[str, str], chunk_rows, key: str) -> list[sa.Row]:
    return chunk_rows(corpus[key])


def test_every_source_is_indexed_with_its_source_type(corpus, chunk_rows) -> None:
    kinds = {key: {r.source_type for r in _rows(corpus, chunk_rows, key)} for key in corpus}
    assert kinds["a_q4"] == kinds["a_q1"] == kinds["b_q4"] == {"transcript"}
    assert kinds["a_xbrl"] == kinds["a_pdf"] == {"results"}  # acceptance 15


def test_acceptance_11_annual_reports_are_not_indexed_and_are_reported_deferred(
    corpus, db: None, embedder
) -> None:
    with transaction() as conn:
        count = conn.execute(
            sa.select(sa.func.count()).where(tables.chunks.c.filing_id == corpus["a_ar"])
        ).scalar()
    assert count == 0
    reports = {r.filing_id: r for r in run_index(embedder=embedder)}
    assert reports[corpus["a_ar"]].status == "deferred"
    assert reports[corpus["a_ar"]].detail == "deferred"


def test_acceptance_2_pages_and_xbrl_citations(corpus, chunk_rows) -> None:
    for key in ("a_q4", "a_pdf"):
        for row in _rows(corpus, chunk_rows, key):
            assert row.page is not None and f"-p{row.page}-" in row.chunk_id
    for row in _rows(corpus, chunk_rows, "a_xbrl"):
        assert row.page is None and row.chunk_id.startswith(f"CHK-{corpus['a_xbrl']}-x-")
        assert json.loads(row.meta_json)["concept"].startswith("in-capmkt:")


def test_pdf_chunks_take_their_basis_from_the_page_heading(corpus, chunk_rows) -> None:
    """Filing basis is standalone; the synthetic auditor report names consolidated results and the
    press release has no basis. XBRL and transcript chunks keep the filing's basis."""
    got = {
        (r.section, json.loads(r.meta_json)["basis"]) for r in _rows(corpus, chunk_rows, "a_pdf")
    }
    assert got == {
        ("results_notes", "standalone"),
        ("auditor_report", "consolidated"),
        ("press_release", "unknown"),
    }
    for key in ("a_xbrl", "a_q4"):
        assert {json.loads(r.meta_json)["basis"] for r in _rows(corpus, chunk_rows, key)} == {
            "standalone"
        }


def test_chunks_copy_the_period_of_their_filing(corpus, chunk_rows) -> None:
    q4 = {(r.period_start, r.period_end, r.period_type) for r in _rows(corpus, chunk_rows, "a_q4")}
    q1 = {(r.period_start, r.period_end, r.period_type) for r in _rows(corpus, chunk_rows, "a_q1")}
    assert q4 == {("2026-01-01", "2026-03-31", "Q")}
    assert q1 == {("2026-04-01", "2026-06-30", "Q")}
    assert {r.fiscal_year for r in _rows(corpus, chunk_rows, "a_q4")} == {"FY26"}
    assert {r.fiscal_year for r in _rows(corpus, chunk_rows, "a_q1")} == {"FY27"}


def test_acceptance_14_speakers_on_transcript_chunks_only(corpus, chunk_rows) -> None:
    for row in _rows(corpus, chunk_rows, "a_q4"):
        assert row.speaker and row.speaker_role in ("management", "analyst", "moderator")
    for key in ("a_xbrl", "a_pdf"):
        assert all(
            r.speaker is None and r.speaker_role is None for r in _rows(corpus, chunk_rows, key)
        )
    qa = [r for r in _rows(corpus, chunk_rows, "a_q4") if r.section == "qa"]
    assert qa
    # the firm is the analyst's: on the analyst's turns only, never on management's replies
    for r in qa:
        has_firm = bool(json.loads(r.meta_json).get("firm"))
        assert has_firm == (r.speaker_role == "analyst")
    assert {r.speaker_role for r in qa} == {"analyst", "management"}


def test_acceptance_7_reindexing_creates_no_duplicates(corpus, chunk_rows, embedder) -> None:
    before = [(r.chunk_id, r.text) for r in chunk_rows()]
    first = run_index(embedder=embedder)
    assert {r.status for r in first} <= {"unchanged", "deferred", "superseded"}
    run_index(reindex=True, embedder=embedder)
    after = [(r.chunk_id, r.text) for r in chunk_rows()]
    assert after == before
    assert len({cid for cid, _ in after}) == len(after)


def test_unchanged_chunks_are_not_embedded_again(corpus, embedder) -> None:
    embedder.calls.clear()
    run_index(reindex=True, embedder=embedder)
    assert embedder.calls == []  # same text, same model: every vector is reused


def test_a_new_filing_embeds_only_its_own_chunks(
    corpus, db: None, fx: ModuleType, embedder, add_filing
) -> None:
    embedder.calls.clear()
    new = add_filing(
        ISIN_A,
        "earnings_call_transcript",
        "pdf",
        "Q4FY26",
        basis="consolidated",
        pages=fx.transcript_pages(),
        key="new",
    )
    run_index(embedder=embedder)
    with transaction() as conn:
        added = conn.execute(
            sa.select(sa.func.count()).where(tables.chunks.c.filing_id == new)
        ).scalar()
    assert embedder.calls == [added]


def test_a_revision_supersedes_its_original(
    db: None, fx: ModuleType, embedder, add_filing, chunk_rows
) -> None:
    facts = fx.text_facts()
    original = add_filing(ISIN_A, "financial_results", "xbrl", text_facts=facts, key="o")
    revised = [dict(f) for f in facts]
    for fact in revised:
        if fact["concept"].endswith("SegmentsExplanatoryTextBlock") and not fact["dimensions"]:
            fact["value"] = "The Company operates in two segments after the revision."
    revision = add_filing(
        ISIN_A,
        "financial_results",
        "xbrl",
        submission="revision",
        revision_no=1,
        filed_on="2026-05-16",
        text_facts=revised,
        key="r",
    )
    run_index(embedder=embedder)
    assert chunk_rows(original) == []  # not indexed: a later version exists
    texts = " ".join(r.text for r in chunk_rows(revision))
    assert "two segments after the revision" in texts and "single segment" not in texts


def test_an_original_indexed_first_loses_its_chunks_when_the_revision_arrives(
    db: None, fx: ModuleType, embedder, add_filing, chunk_rows
) -> None:
    original = add_filing(ISIN_A, "financial_results", "xbrl", text_facts=fx.text_facts(), key="o")
    run_index(embedder=embedder)
    assert chunk_rows(original)
    add_filing(
        ISIN_A,
        "financial_results",
        "xbrl",
        submission="revision",
        revision_no=1,
        filed_on="2026-05-16",
        text_facts=fx.text_facts(),
        key="r",
    )
    reports = {r.filing_id: r.status for r in run_index(embedder=embedder)}
    assert reports[original] == "superseded" and chunk_rows(original) == []


def test_quarantined_filings_are_never_indexed(
    db: None, fx: ModuleType, embedder, add_filing, chunk_rows
) -> None:
    quarantined = add_filing(
        ISIN_A, "financial_results", "xbrl", status="quarantined", text_facts=fx.text_facts()
    )
    assert run_index(embedder=embedder) == []
    assert chunk_rows(quarantined) == []


def test_a_filing_without_text_blocks_gets_a_summary_listing_what_is_absent(
    db: None, embedder, add_filing, chunk_rows
) -> None:
    filing = add_filing(ISIN_A, "financial_results", "xbrl", text_facts=[])
    (report,) = run_index(embedder=embedder)
    assert report.status == "indexed" and report.chunks == 0
    summary = json.loads(
        (get_settings().paths.processed / filing / SUMMARY_FILE).read_text("utf-8")
    )
    assert len(summary["absent_concepts"]) == 4 and chunk_rows(filing) == []


def test_acceptance_10_summary_account_for_every_page(corpus) -> None:
    path = get_settings().paths.processed / corpus["a_pdf"] / SUMMARY_FILE
    summary = json.loads(path.read_text(encoding="utf-8"))
    assert summary["pages_kept"] + summary["pages_skipped"] == summary["pages_total"] == 12
    assert {s["reason"] for s in summary["skipped"]} <= {"not_indexed", "unknown_section"}
    transcript = json.loads(
        (get_settings().paths.processed / corpus["a_q4"] / SUMMARY_FILE).read_text("utf-8")
    )
    assert transcript["pages_kept"] + transcript["pages_skipped"] == transcript["pages_total"] == 5
    assert transcript["skipped"][0] == {
        "start_page": 1,
        "end_page": 2,
        "reason": "not_indexed",
        "detail": "cover letter / title page",
    }


def test_acceptance_10_with_logging_off_only_totals_are_kept(
    db: None, fx: ModuleType, embedder, add_filing, chunk_rows
) -> None:
    import yaml

    from finrag.core import config

    data = yaml.safe_load(config.SETTINGS_PATH.read_text(encoding="utf-8"))
    data["narrative"]["log_skipped_sections"] = False
    config.SETTINGS_PATH.write_text(yaml.safe_dump(data), encoding="utf-8")
    config.reset_settings_cache()
    filing = add_filing(ISIN_A, "financial_results", "pdf", pages=fx.results_pages())
    run_index(embedder=embedder)
    summary = json.loads(
        (get_settings().paths.processed / filing / SUMMARY_FILE).read_text("utf-8")
    )
    assert "skipped" not in summary and summary["pages_skipped"] == 3
    assert chunk_rows(filing)  # the notes pages are indexed all the same


def test_indexes_are_written_per_company(corpus) -> None:
    folder = get_settings().paths.indexes
    for isin in (ISIN_A, "INE222B01028"):
        assert {p.name for p in (folder / isin).iterdir()} == {
            "vectors.npy",
            "ids.json",
            "bm25.json",
        }
