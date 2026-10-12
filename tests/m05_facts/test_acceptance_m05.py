"""M05 acceptance criteria (one test per criterion in docs/modules/M05_facts.md)."""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa

from finrag.core.db import tables, transaction
from finrag.core.types import Period, PeriodType
from finrag.facts import dependencies
from finrag.facts.current_view import get_fact, get_facts, history
from finrag.facts.derivation import derive_groups, derive_isin

ISIN = "INE111A01010"
BASIS = "consolidated"


def period(kit, name: str) -> Period:
    start, end, kind = kit.FY26[name]
    return Period(start, end, PeriodType(kind))


def count(table) -> int:
    with transaction() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar()


def current(kit, name: str, concept: str = "revenue", **kw):
    return get_fact(ISIN, concept, period(kit, name), BASIS, **kw)


def test_1_same_candidates_twice_adds_nothing(kit) -> None:
    kit.add_filing("FIL-a")
    cands = [kit.cand("FIL-a", "Q1", "100"), kit.cand("FIL-a", "Q2", "120")]
    first = kit.write(cands)
    second = kit.write(cands)
    assert (first.inserted, second.inserted, second.skipped) == (2, 0, 2)
    assert count(tables.canonical_facts) == 2


def test_1a_xbrl_beats_pdf_and_pdf_fills_gaps(kit) -> None:
    kit.add_filing("FIL-x", filed_on="2026-05-01")
    kit.add_filing("FIL-p", filed_on="2026-09-01", fmt="pdf")
    kit.write(
        [kit.cand("FIL-p", "Q4", "99.9", extractor="ocr", confidence=0.99, filed_on="2026-09-01")]
    )
    assert current(kit, "Q4").value == kit.D("99.9")  # no XBRL: the PDF/OCR fact fills the gap
    kit.write([kit.cand("FIL-x", "Q4", "100")])
    assert current(kit, "Q4").value == kit.D("100")  # XBRL wins although the PDF is later
    assert len(history(ISIN, "revenue", period(kit, "Q4"), BASIS)) == 2  # both rows stay
    assert count(tables.restatements) == 0  # XBRL vs PDF/OCR is never a restatement


def test_2_later_filing_wins_and_as_of_returns_earlier(kit) -> None:
    kit.add_filing("FIL-1", filed_on="2026-05-01")
    kit.add_filing("FIL-2", filed_on="2026-08-01")
    kit.write([kit.cand("FIL-1", "Q1", "100")])
    kit.write([kit.cand("FIL-2", "Q1", "110", filed_on="2026-08-01")])
    assert len(history(ISIN, "revenue", period(kit, "Q1"), BASIS)) == 2
    assert current(kit, "Q1").value == kit.D("110")
    assert current(kit, "Q1", as_of=date(2026, 6, 1)).value == kit.D("100")
    assert current(kit, "Q1", as_of=date(2026, 4, 1)) is None


def test_3_restatement_tolerances(kit) -> None:
    for i, fid in enumerate(("FIL-1", "FIL-2"), 1):
        kit.add_filing(fid, filed_on=f"2026-0{i + 4}-01")
    # 0.3% of 10 bn rupees = 30 mn rupees: above 1 crore but under 0.5%
    kit.write([kit.cand("FIL-1", "Q1", "10000000000", "revenue")])
    kit.write([kit.cand("FIL-2", "Q1", "10030000000", "revenue", filed_on="2026-06-01")])
    # 2% and 20 crore: flagged
    kit.write([kit.cand("FIL-1", "Q2", "10000000000", "revenue")])
    kit.write([kit.cand("FIL-2", "Q2", "10200000000", "revenue", filed_on="2026-06-01")])
    # 2% but only 0.5 crore: relative exceeded, absolute not
    kit.write([kit.cand("FIL-1", "Q3", "250000000", "revenue")])
    kit.write([kit.cand("FIL-2", "Q3", "255000000", "revenue", filed_on="2026-06-01")])
    with transaction() as conn:
        rows = {r.period_end: r.flagged for r in conn.execute(sa.select(tables.restatements))}
    assert rows == {"2025-06-30": 0, "2025-09-30": 1, "2025-12-31": 0}


def test_4_derives_q2_from_h1_and_q1(kit) -> None:
    kit.add_filing("FIL-1")
    kit.write([kit.cand("FIL-1", "H1", "400"), kit.cand("FIL-1", "Q1", "150")])
    stats = derive_isin(ISIN)
    assert stats.inserted == 1
    q2 = current(kit, "Q2")
    assert q2.value == kit.D("250") and q2.is_derived
    inputs = {i["period"]: i["sign"] for i in q2.derivation["inputs"]}
    assert inputs == {"H1 FY26": 1, "Q1 FY26": -1}
    ids = {f.fact_id for f in get_facts(ISIN, ["revenue"]) if not f.is_derived}
    assert set(q2.derivation["input_fact_ids"]) == ids
    assert derive_isin(ISIN).inserted == 0  # idempotent


def test_5_reported_q2_is_not_derived(kit) -> None:
    kit.add_filing("FIL-1")
    kit.write([kit.cand("FIL-1", p, v) for p, v in (("H1", "400"), ("Q1", "150"), ("Q2", "251"))])
    assert derive_isin(ISIN).inserted == 0
    assert not current(kit, "Q2").is_derived and current(kit, "Q2").value == kit.D("251")


def test_6_missing_q1_derives_nothing_and_does_not_fail(kit) -> None:
    kit.add_filing("FIL-1")
    kit.write([kit.cand("FIL-1", "H1", "400")])
    assert derive_isin(ISIN).inserted == 0
    assert current(kit, "Q2") is None


def test_7_restated_q1_reports_derived_q2_and_rederives(kit) -> None:
    kit.add_filing("FIL-1", filed_on="2026-05-01")
    kit.add_filing("FIL-2", filed_on="2026-08-01")
    kit.write([kit.cand("FIL-1", "H1", "400"), kit.cand("FIL-1", "Q1", "150")])
    derive_isin(ISIN)
    old_q2 = current(kit, "Q2")
    written = kit.write([kit.cand("FIL-2", "Q1", "160", filed_on="2026-08-01")])
    hit = dependencies.affected(written.fact_ids)
    assert hit.derived_fact_ids == (old_q2.fact_id,)
    assert any(
        k.period_type is PeriodType.Q and k.period_end == date(2025, 9, 30) for k in hit.periods
    )
    derive_groups(hit.rederive)
    q2 = current(kit, "Q2")
    assert q2.value == kit.D("240") and q2.fact_id != old_q2.fact_id
    assert len(history(ISIN, "revenue", period(kit, "Q2"), BASIS)) == 2
    with transaction() as conn:
        derived_restatement = conn.execute(
            sa.select(tables.restatements).where(tables.restatements.c.new_fact_id == q2.fact_id)
        ).first()
    assert derived_restatement is not None and derived_restatement.old_fact_id == old_q2.fact_id


def test_8_instants_are_never_derived(kit, real_taxonomy) -> None:
    kit.add_filing("FIL-1")
    cands = []
    for name, value in (("H1", "400"), ("Q1", "150")):
        c = kit.cand("FIL-1", name, value, "total_assets")
        start, end, _ = kit.FY26[name]
        cands.append(
            type(c)(
                **{**c.__dict__, "period_start": end, "period_end": end, "period_type": "INSTANT"}
            )
        )
    kit.write(cands)
    assert derive_groups([(ISIN, "total_assets", BASIS)], taxonomy=real_taxonomy).inserted == 0
