"""Checks against the real filings (skipped until the real database and page text exist).

The real data/ folder is never written: the database and the needed processed files are copied to
a temp folder, migrated, indexed there with the real embedding model (from the local cache, no
network), and searched. Expected analyst lists were read off the four real transcripts by hand.
"""

from __future__ import annotations

import json
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa

from finrag.core.config import Settings, get_settings
from finrag.core.db import _make_engine, init_db, tables
from finrag.core.fiscal import label_to_period
from finrag.core.types import Period
from finrag.narrative.embedder import SentenceTransformerEmbedder
from finrag.narrative.patterns import load_narrative_patterns
from finrag.narrative.runner import analyse_results_pdf, run_index
from finrag.narrative.search import SearchHit, SearchRequest, search
from finrag.narrative.sectioner import spans_of
from finrag.narrative.transcript_sectioner import section_transcript
from finrag.parsing.pdf_text import read_pages_jsonl

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data" / "finrag.db"
HDFC, NETWEB, GABRIEL = "INE040A01034", "INE0NT901020", "INE524A01029"

# (company, quarter) -> [(analyst as introduced, firm or None), ...] in call order
ANALYSTS = {
    (HDFC, "Q4 FY26"): [
        ("Mahrukh Adajania", "Tara Capital Partners"),
        ("Nitin Aggarwal", "Motilal Oswal"),
        ("Kunal Shah", "Citigroup"),
        ("Pranav", "Bernstein"),
        ("Seshadri Sen", "Emkay Global"),
        ("Rikin Shah", "IIFL Capital"),
        ("Abhishek Murarka", "HSBC"),
    ],
    (HDFC, "Q1 FY27"): [
        ("Mahrukh Adajania", "Tara Capital Partners"),
        ("Pranav Gundlapalle", "Bernstein"),
        ("Kunal Shah", "Citigroup"),
        ("Seshadri Sen", "Emkay Global"),
        ("Suresh Ganapathy", "Macquarie Capital"),  # the firm wraps over two lines in the PDF
        ("Abhishek Murarka", "HSBC"),
        ("Nitin Aggarwal", "Motilal Oswal"),
        ("Piran Engineer", "CLSA India"),
    ],
    (NETWEB, "Q4 FY26"): [
        ("Renu Baid", "IIFL Securities"),
        ("Seema Nayak", "ICICI Securities"),
        ("Harinder Singh", "Blackstone"),
        ("Akshay", "AK Investments"),
        ("Chi Ho Wong", "Pictet Asset Management"),
        ("Sandeep Shah", "Equirus Securities"),
        ("Vinay Menon", "Monarch Networth Capital"),
        ("Pooja Seth", "Yes Securities"),
        ("Mansimer Singh Sethi", "Citi Capital"),
        ("Vatsal Aggarwal", None),  # "from -- who is a Retail Investor": no firm
        ("Prajay", "Wealthyvia"),
    ],
    (NETWEB, "Q1 FY27"): [
        ("Renu Baid", "IIFL Capital"),
        ("Divyesh Mehta", "Invesco India Mutual Fund"),
        ("Jatin Kalra", "Bank of America"),
        ("Sandeep Shah", "Equirus Securities"),
        ("Vinay", "Monarch Capital"),
        ("Rohit", "CLSA"),
        ("Akshay", "AK Investment"),
        ("Mansimer Singh Sethi", "Citi Capital"),
        ("Omkar", "Shree Investments"),
        ("Jatin Kalra", "Bank of America"),  # re-introduced for a follow-up: a new unit
        ("Jayalaxmi Gupta", "Wealth Company"),
        ("Anuj Kashyap", "A3 Capital"),
        ("Saurabh Sadhwani", "Sahasrar Capital"),
    ],
}


def _model_cached() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        name = get_settings().narrative.embedding_model
        return isinstance(try_to_load_from_cache(name, "config.json"), str)
    except Exception:
        return False


def _has_real_data() -> bool:
    if not DB.exists():
        return False
    engine = sa.create_engine(f"sqlite:///file:{DB.as_posix()}?mode=ro&uri=true")
    try:
        with engine.connect() as conn:
            transcripts = conn.execute(
                sa.text(
                    "select count(*) from filings where filing_type='earnings_call_transcript' "
                    "and status in ('parsed','normalized')"
                )
            ).scalar()
        return bool(transcripts) and transcripts >= 4 and _model_cached()
    except sa.exc.OperationalError:
        return False
    finally:
        engine.dispose()


pytestmark = [
    pytest.mark.golden,
    pytest.mark.slow,
    pytest.mark.skipif(not _has_real_data(), reason="real filings / embedding model not available"),
]


@dataclass
class Real:
    """The indexed copy of the real data."""

    engine: sa.Engine
    settings: Settings
    embedder: SentenceTransformerEmbedder
    filings: list[sa.Row]

    def find(self, isin: str, filing_type: str, fmt: str, period_end: str) -> sa.Row:
        """The one indexable filing of this company, type, format and period end."""
        rows = [
            r
            for r in self.filings
            if (r.isin, r.filing_type, r.format, r.period_end)
            == (isin, filing_type, fmt, period_end)
        ]
        assert len(rows) >= 1, (isin, filing_type, fmt, period_end)
        return rows[-1]

    def search(self, isin: str, query: str, **kwargs) -> list[SearchHit]:
        request = SearchRequest(isin=isin, query_text=query, **{"k": 10, **kwargs})
        return search(request, self.embedder, self.settings, self.engine)


@pytest.fixture(scope="module")
def real(tmp_path_factory) -> Real:
    work = tmp_path_factory.mktemp("m08_golden")
    db_copy = work / "finrag.db"
    shutil.copy(DB, db_copy)
    engine = _make_engine(db_copy)
    init_db(engine)
    with engine.begin() as conn:  # the real database may already hold chunks: index from scratch
        conn.execute(sa.delete(tables.chunks))
    base = get_settings()
    paths = base.paths.model_copy(
        update={"db": db_copy, "processed": work / "processed", "indexes": work / "indexes"}
    )
    settings = base.model_copy(update={"paths": paths})
    with engine.connect() as conn:
        filings = list(
            conn.execute(
                sa.select(tables.filings)
                .where(tables.filings.c.status.in_(("parsed", "normalized")))
                .order_by(tables.filings.c.created_at)
            )
        )
    for row in filings:
        source = base.paths.processed / row.filing_id
        for name in ("pages.jsonl", "text_facts.jsonl"):
            if (source / name).exists():
                (work / "processed" / row.filing_id).mkdir(parents=True, exist_ok=True)
                shutil.copy(source / name, work / "processed" / row.filing_id / name)
    previous = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"  # the model must come from the local cache
    try:
        embedder = SentenceTransformerEmbedder(settings.narrative)
        run_index(engine=engine, settings=settings, embedder=embedder)
        yield Real(engine, settings, embedder, filings)
    finally:
        if previous is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = previous
        engine.dispose()


QUARTERS = {"Q4 FY26": "2026-03-31", "Q1 FY27": "2026-06-30"}


def _transcript(real: Real, isin: str, quarter: str):
    row = real.find(isin, "earnings_call_transcript", "pdf", QUARTERS[quarter])
    pages = read_pages_jsonl(real.settings.paths.processed / row.filing_id / "pages.jsonl")
    return section_transcript(pages, load_narrative_patterns().transcript)


@pytest.mark.parametrize("key", list(ANALYSTS))
def test_acceptance_16_transcript_sections_speakers_and_analysts(real: Real, key) -> None:
    isin, quarter = key
    result = _transcript(real, isin, quarter)
    sections = [u.section for u in result.units]
    assert sections == sorted(sections, key=["prepared_remarks", "qa", "closing_remarks"].index)
    assert {"prepared_remarks", "qa", "closing_remarks"} <= set(sections)
    qa = [u for u in result.units if u.section == "qa"]
    assert [(u.speaker, u.firm) for u in qa] == ANALYSTS[key]  # Q&A starts at the first analyst
    assert all(u.speaker_role == "analyst" for u in qa)
    assert not [u for u in result.units if u.speaker == "Moderator"]  # the operator is excluded
    assert result.summary.extra["null_role_turns"] == []
    closing = [u for u in result.units if u.section == "closing_remarks"]
    assert closing and all(u.speaker_role == "management" for u in closing)


def test_netweb_host_is_moderator_in_prepared_remarks_and_analyst_in_the_q4_qa(real: Real) -> None:
    result = _transcript(real, NETWEB, "Q4 FY26")
    host_prepared = [
        u for u in result.units if u.section == "prepared_remarks" and u.speaker == "Seema Nayak"
    ]
    host_qa = [u for u in result.units if u.section == "qa" and u.speaker == "Seema Nayak"]
    assert host_prepared and host_prepared[0].speaker_role == "moderator"
    assert len(host_qa) == 1 and host_qa[0].speaker_role == "analyst"
    assert host_qa[0].firm == "ICICI Securities"


@pytest.mark.parametrize("quarter,non_bold", [("Q4 FY26", []), ("Q1 FY27", ["Hirdey Vikram"])])
def test_netweb_unattributed_turn_count(real: Real, quarter: str, non_bold: list[str]) -> None:
    """Both Netweb calls: no turn is left without a speaker or a role."""
    result = _transcript(real, NETWEB, quarter)
    extra = result.summary.extra
    assert extra["unattributed_turns"] == [] and extra["null_role_turns"] == []
    assert extra.get("label_lost_after_intro") is None
    # Q1 FY27 page 14: "Hirdey Vikram:" is in the text layer but not bold; he is on the cover list
    assert [b["speaker"] for b in extra.get("non_bold_labels", [])] == non_bold


def test_netweb_note_5_inventory_valuation_change_is_retrievable(real: Real) -> None:
    pdf = real.find(NETWEB, "financial_results", "pdf", "2026-03-31")
    hits = real.search(
        NETWEB,
        "change in inventory valuation method",
        source_types=["results"],
        periods=[label_to_period("Q4 FY26")],
        k=3,
    )
    top = next(h for h in hits if "moving weighted average cost" in h.text)
    assert (top.filing_id, top.page, top.section) == (pdf.filing_id, 9, "results_notes")
    assert "Ind AS 8" in top.text and top.period_label == "Q4 FY26"


def test_hdfc_asset_quality_returns_only_hdfc_transcripts_of_the_right_quarter(real: Real) -> None:
    query = "asset quality gross NPA slippages and credit cost"
    both = real.search(HDFC, query, source_types=["transcript"])
    assert both and {h.source_type for h in both} == {"transcript"}
    owner = dict(
        real.engine.connect()
        .execute(sa.select(tables.chunks.c.chunk_id, tables.chunks.c.isin))
        .all()
    )
    assert {owner[h.chunk_id] for h in both} == {HDFC}
    for quarter in QUARTERS:
        hits = real.search(
            HDFC, query, source_types=["transcript"], periods=[label_to_period(quarter)]
        )
        assert hits and {h.period_label for h in hits} == {quarter}
        assert {h.period_end for h in hits} == {QUARTERS[quarter]}
        assert any("NPA" in h.text or "asset quality" in h.text.lower() for h in hits)


def test_no_chunk_crosses_companies_or_ignores_the_period_filter(real: Real) -> None:
    c = tables.chunks
    with real.engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                c.c.chunk_id,
                c.c.isin,
                c.c.period_start,
                c.c.period_end,
                c.c.period_type,
                tables.filings.c.isin.label("f_isin"),
                tables.filings.c.period_end.label("f_end"),
                tables.filings.c.period_start.label("f_start"),
                tables.filings.c.period_type.label("f_type"),
            ).join(tables.filings, tables.filings.c.filing_id == c.c.filing_id)
        ).all()
    assert rows
    for row in rows:  # every chunk carries exactly its own filing's company and period
        assert (row.isin, row.period_start, row.period_end, row.period_type) == (
            row.f_isin,
            row.f_start,
            row.f_end,
            row.f_type,
        )
    by_company = defaultdict(set)
    for row in rows:
        by_company[row.isin].add((row.period_start, row.period_end, row.period_type))
    queries = ["revenue growth and margins", "dividend", "auditor opinion", "segment performance"]
    for isin, periods in by_company.items():
        for start, end, kind in sorted(periods):
            period = Period(date.fromisoformat(start), date.fromisoformat(end), kind)
            for text in queries:
                for hit in real.search(isin, text, periods=[period], k=20):
                    assert (hit.period_start, hit.period_end, hit.period_type) == (start, end, kind)
                    assert {r.isin for r in rows if r.chunk_id == hit.chunk_id} == {isin}


def test_results_pdfs_have_notes_and_auditor_pages_with_readable_text(real: Real) -> None:
    patterns = load_narrative_patterns()
    pdfs = [r for r in real.filings if r.filing_type == "financial_results" and r.format == "pdf"]
    assert len(pdfs) >= 11
    for row in pdfs:
        slices, summary, _ = analyse_results_pdf(
            real.settings.paths.processed / row.filing_id, real.settings, patterns
        )
        found = {s.section for s in spans_of(slices)}
        assert {"results_notes", "auditor_report"} <= found, (row.filing_id, found)
        assert (row.isin == HDFC) == ("press_release" in found), (row.filing_id, found)
        assert min(s.quality for s in slices) >= 0.70
        assert summary.pages_kept + summary.pages_skipped == summary.pages_total
    netweb = real.find(NETWEB, "financial_results", "pdf", "2026-03-31")
    slices, _, _ = analyse_results_pdf(
        real.settings.paths.processed / netweb.filing_id, real.settings, patterns
    )
    assert [(s.section, s.start_page, s.end_page) for s in spans_of(slices)] == [
        ("auditor_report", 3, 5),
        ("results_notes", 9, 9),
    ]


def _pdf_chunks(real: Real, isin: str) -> list[sa.Row]:
    """Chunks of a company's results PDFs with their basis and section."""
    c, f = tables.chunks, tables.filings
    with real.engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(c.c.filing_id, c.c.section, c.c.page, c.c.meta_json, f.c.period_end)
                .join(f, f.c.filing_id == c.c.filing_id)
                .where(
                    c.c.isin == isin, f.c.format == "pdf", f.c.filing_type == "financial_results"
                )
                .order_by(c.c.filing_id, c.c.page)
            )
        )


def test_hdfc_pdf_chunks_take_their_basis_from_the_page_headings(real: Real) -> None:
    """HDFC's results PDF holds the standalone results, then the consolidated results, then a
    press release: the filing says consolidated, the pages say otherwise."""
    rows = _pdf_chunks(real, HDFC)
    assert rows
    for filing in {r.filing_id for r in rows}:
        mine = [
            (r.page, json.loads(r.meta_json)["basis"], r.section)
            for r in rows
            if r.filing_id == filing
        ]
        bases = [b for _, b, _ in mine]
        assert {"standalone", "consolidated", "unknown"} == set(bases), filing
        order = [b for i, b in enumerate(bases) if i == 0 or b != bases[i - 1]]
        assert order == ["standalone", "consolidated", "unknown"], (filing, order)  # one block each
        assert {sec for _, b, sec in mine if b == "unknown"} == {"press_release"}
        assert {sec for _, b, sec in mine if b != "unknown"} <= {"results_notes", "auditor_report"}


def test_gabriel_and_netweb_pdf_chunks_have_a_basis_too(real: Real) -> None:
    for isin, expected in ((NETWEB, {"standalone"}), (GABRIEL, {"standalone", "consolidated"})):
        got = {json.loads(r.meta_json)["basis"] for r in _pdf_chunks(real, isin)}
        assert expected <= got <= {"standalone", "consolidated"}, (isin, got)


def test_hdfc_press_release_is_indexed_as_management_commentary_on_asset_quality(
    real: Real,
) -> None:
    pdf = real.find(HDFC, "financial_results", "pdf", "2026-06-30")
    hits = real.search(
        HDFC,
        "gross NPA ratio and slippages in the quarter",
        sections=["press_release"],
        periods=[label_to_period("Q1 FY27")],
        k=5,
    )
    assert hits and {h.filing_id for h in hits} == {pdf.filing_id}
    assert {(h.section, h.source_type, h.basis) for h in hits} == {
        ("press_release", "results", "unknown")
    }
    assert any("NPA" in h.text or "non-performing" in h.text.lower() for h in hits)
    again = real.search(
        HDFC, "gross NPA ratio", sections=["press_release"], periods=[label_to_period("Q4 FY26")]
    )
    assert again and {h.period_label for h in again} == {"Q4 FY26"}


def test_annual_reports_are_not_indexed(real: Real) -> None:
    annual = [r.filing_id for r in real.filings if r.filing_type == "annual_report"]
    assert annual
    with real.engine.connect() as conn:
        count = conn.execute(
            sa.select(sa.func.count()).where(tables.chunks.c.filing_id.in_(annual))
        ).scalar()
    assert count == 0


def test_the_quarantined_originals_are_not_indexed(real: Real) -> None:
    with real.engine.connect() as conn:
        quarantined = (
            conn.execute(
                sa.select(tables.filings.c.filing_id).where(
                    tables.filings.c.status == "quarantined"
                )
            )
            .scalars()
            .all()
        )
        count = conn.execute(
            sa.select(sa.func.count()).where(tables.chunks.c.filing_id.in_(quarantined))
        ).scalar()
    assert count == 0
