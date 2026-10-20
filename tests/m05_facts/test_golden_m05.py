"""Checks against the real canonical_facts in data/finrag.db (skipped until it is populated).

The real database is opened read-only; nothing is written.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy.engine import Engine

from finrag.core.db import tables
from finrag.core.types import Period, PeriodType
from finrag.facts.current_view import current_of, get_fact, get_facts, load_facts
from finrag.normalize.taxonomy import parse_taxonomy

REAL = Path(__file__).resolve().parents[2]
DB = REAL / "data" / "finrag.db"
NETWEB, GABRIEL, HDFC = "INE0NT901020", "INE524A01029", "INE040A01034"
MN = Decimal(1_000_000)
FY26 = Period(date(2025, 4, 1), date(2026, 3, 31), PeriodType.FY)
H1_26 = Period(date(2025, 4, 1), date(2025, 9, 30), PeriodType.H)
H2_26 = Period(date(2025, 10, 1), date(2026, 3, 31), PeriodType.H)
Q2_26 = Period(date(2025, 7, 1), date(2025, 9, 30), PeriodType.Q)
Q4_26 = Period(date(2026, 1, 1), date(2026, 3, 31), PeriodType.Q)
NETWEB_PDF = "FIL-84bf2a7b31af"
NETWEB_QUARANTINED = ("FIL-24ed09cfcdda", "FIL-58965e306312")
GABRIEL_ORIGINAL, GABRIEL_REVISION = "FIL-10e6bb5129cb", "FIL-d656339b02dc"


def _engine() -> Engine | None:
    """Read-only engine on the real database, or None when it has no canonical facts."""
    if not DB.exists():
        return None
    engine = sa.create_engine(f"sqlite:///file:{DB.as_posix()}?mode=ro&uri=true")
    try:
        with engine.connect() as conn:
            found = conn.execute(
                sa.select(sa.func.count()).select_from(tables.canonical_facts)
            ).scalar()
    except sa.exc.OperationalError:
        return None
    return engine if found else None


ENGINE = _engine()
pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(ENGINE is None, reason="real canonical_facts not populated"),
]


@pytest.fixture(scope="module")
def taxonomy():
    return parse_taxonomy(yaml.safe_load((REAL / "config" / "taxonomy.yaml").read_text("utf-8")))


def _all(isin: str, concept: str, basis: str):
    with ENGINE.connect() as conn:
        return load_facts(conn, isin, [concept], basis)


def test_gabriel_revision_is_current_and_the_original_is_flagged() -> None:
    cfo = get_fact(GABRIEL, "cfo", FY26, "consolidated", engine=ENGINE)
    assert cfo.source_filing_id == GABRIEL_REVISION and cfo.value == Decimal("3452.74") * MN
    # the revision's own filed_on was corrected to the NSE listing date
    assert cfo.filed_on == "2026-06-29"
    original = get_fact(GABRIEL, "cfo", FY26, "consolidated", date(2026, 6, 1), ENGINE)
    assert original.source_filing_id == GABRIEL_ORIGINAL
    assert original.value == Decimal("4435.59") * MN
    r = tables.restatements
    with ENGINE.connect() as conn:
        rows = conn.execute(sa.select(r).where(r.c.isin == GABRIEL, r.c.concept == "cfo")).all()
        old_value = conn.execute(
            sa.select(tables.canonical_facts.c.value).where(
                tables.canonical_facts.c.fact_id == rows[0].old_fact_id
            )
        ).scalar()
    assert len(rows) == 1 and rows[0].flagged == 1 and Decimal(old_value) == Decimal("4435.59") * MN


def test_gabriel_unchanged_facts_are_not_flagged_and_pl_has_no_restatement(taxonomy) -> None:
    """Between XBRL versions only the FY26 revision's cash flow lines differ, and the Q1 FY26
    standalone revision (it only fixes face value and EPS tags) is recorded but not flagged.
    (PDF-against-PDF differences, e.g. comparatives restated for the scheme, are separate rows.)"""
    old, new = tables.canonical_facts.alias("old"), tables.canonical_facts.alias("new")
    r = tables.restatements
    with ENGINE.connect() as conn:
        rows = conn.execute(
            sa.select(
                r, old.c.source_class.label("old_class"), new.c.source_class.label("new_class")
            )
            .join(old, old.c.fact_id == r.c.old_fact_id)
            .join(new, new.c.fact_id == r.c.new_fact_id)
            .where(r.c.isin == GABRIEL, old.c.source_class == "xbrl", new.c.source_class == "xbrl")
        ).all()
    assert {r.concept for r in rows} == {
        "cfo", "cfi", "cff", "capex_ppe", "capex_intangibles", "dividends_paid",
        "net_change_in_cash", "opening_cash", "closing_cash", "eps_basic", "eps_diluted",
    }  # fmt: skip
    pl = [r for r in rows if taxonomy.concepts[r.concept].statement == "profit_and_loss"]
    assert {r.concept for r in pl} == {"eps_basic", "eps_diluted"}
    assert all(r.flagged == 0 and r.period_end == "2025-06-30" for r in pl)  # 0 -> 3.88 EPS tag
    assert {r.concept for r in rows if r.flagged} == {
        "cfo", "cfi", "cff", "capex_ppe", "capex_intangibles",
    }  # fmt: skip
    # a P&L fact is stored once per filing and served from the revision
    revenue = _all(GABRIEL, "revenue", "consolidated")
    assert {f.source_filing_id for f in revenue if f.period == Q4_26} >= {GABRIEL_REVISION}


def test_netweb_quarantined_originals_contribute_nothing() -> None:
    c = tables.canonical_facts
    with ENGINE.connect() as conn:
        found = conn.execute(
            sa.select(sa.func.count()).where(c.c.source_filing_id.in_(NETWEB_QUARANTINED))
        ).scalar()
    assert found == 0
    revenue = get_fact(NETWEB, "revenue", Q4_26, "standalone", engine=ENGINE)
    assert revenue.source_filing_id == "FIL-3057b3f95ef2"


def test_netweb_xbrl_wins_over_pdf_for_q4_fy26() -> None:
    with ENGINE.connect() as conn:
        # every stored version, including PDF/OCR ones the gap-fill gate refuses: precedence is
        # about which of the versions is current, whatever the gate says about the weaker ones
        facts = load_facts(conn, NETWEB, None, "standalone", include_rejected=True)
    by_key: dict[tuple, list] = {}
    for f in facts:
        by_key.setdefault((f.concept, f.period.start, f.period.end), []).append(f)
    both = [
        v
        for v in by_key.values()
        if {f.source_filing_id for f in v} >= {NETWEB_PDF, "FIL-3057b3f95ef2"}
    ]
    assert len(both) >= 40
    for versions in both:
        top = current_of(versions)[(versions[0].period.start, versions[0].period.end)]
        assert top.source_class.value == "xbrl" and top.source_filing_id == "FIL-3057b3f95ef2"
    pdf_only = [v for v in by_key.values() if {f.source_filing_id for f in v} == {NETWEB_PDF}]
    # comparatives only the scan has (the Q3 FY26 XBRL has since replaced most of them)
    for versions in pdf_only:
        top = current_of(versions)[(versions[0].period.start, versions[0].period.end)]
        assert top.source_class.value in ("pdf", "ocr")


def test_derived_h2_from_real_h1_and_fy(taxonomy) -> None:
    checked = 0
    for isin, basis, concept in (
        (GABRIEL, "consolidated", "revenue"),
        (GABRIEL, "consolidated", "cfo"),
        (HDFC, "consolidated", "profit_after_tax"),
        (NETWEB, "standalone", "revenue"),
        (NETWEB, "standalone", "cfo"),
    ):
        fy = get_fact(isin, concept, FY26, basis, engine=ENGINE)
        h1 = get_fact(isin, concept, H1_26, basis, engine=ENGINE)
        h2 = get_fact(isin, concept, H2_26, basis, engine=ENGINE)
        assert h2 is not None and h2.is_derived, (isin, concept)
        assert h2.value == fy.value - h1.value
        assert h2.derivation["all_xbrl"] is True
        checked += 1
    assert checked == 5


def test_directly_reported_quarters_are_not_derived() -> None:
    # Q2 and Q4 are reported in the Q2 and Q4 filings of every company
    for isin, basis in ((GABRIEL, "consolidated"), (HDFC, "consolidated"), (NETWEB, "standalone")):
        for quarter in (Q2_26, Q4_26):
            versions = [
                f for f in _all(isin, "revenue", basis) if f.period == quarter and f.is_derived
            ]
            assert versions == [], (isin, quarter)


def test_only_derivable_concepts_were_derived(taxonomy) -> None:
    c = tables.canonical_facts
    with ENGINE.connect() as conn:
        concepts = set(
            conn.execute(sa.select(c.c.concept).where(c.c.is_derived == 1).distinct()).scalars()
        )
    assert concepts
    assert all(taxonomy.concepts[n].can_derive for n in concepts)
    assert not concepts & {"eps_basic", "eps_diluted"}
    assert not {n for n in concepts if n.startswith("bank_") and not taxonomy.concepts[n].derivable}
    with ENGINE.connect() as conn:
        instants = conn.execute(
            sa.select(sa.func.count()).where(c.c.is_derived == 1, c.c.period_type == "INSTANT")
        ).scalar()
    assert instants == 0


def test_hdfc_bank_asset_quality_exists_only_on_the_standalone_basis() -> None:
    c = tables.canonical_facts
    with ENGINE.connect() as conn:
        rows = conn.execute(
            sa.select(c.c.concept, c.c.basis).where(
                c.c.isin == HDFC,
                c.c.concept.in_(["bank_gross_npa", "bank_net_npa", "bank_cet1_ratio"]),
            )
        ).all()
    assert rows and {r.basis for r in rows} == {"standalone"}
    assert not any(f.is_derived for f in get_facts(HDFC, ["bank_gross_npa"], engine=ENGINE))


# --- Gabriel's restated Q1 FY26 profit: printed again by the Q1 FY27 results ---------------------

Q1_26 = Period(date(2025, 4, 1), date(2025, 6, 30), PeriodType.Q)
GABRIEL_Q1_FY27_PDF = "FIL-83a2f50f0a6d"


@pytest.mark.parametrize(
    ("basis", "original", "restated", "where"),
    [
        ("consolidated", "619720000", "1054530000.00", "7:38:3"),
        ("standalone", "556710000", "596130000.00", "4:36:3"),
    ],
)
def test_gabriel_q1_fy26_profit_restated_by_the_scheme_is_a_comparative_not_the_current_value(
    basis, original, restated, where
) -> None:
    """The Q1 FY27 PDF prints Q1 FY26 profit again (third value column of the P&L, page 7
    consolidated, page 4 standalone): a native-text comparative, stored, never the current value."""
    versions = [f for f in _all(GABRIEL, "profit_after_tax", basis) if f.period == Q1_26]
    comparatives = [f for f in versions if f.is_pdf_comparative]
    assert [(f.source_filing_id, str(f.value)) for f in comparatives] == [
        (GABRIEL_Q1_FY27_PDF, restated)
    ]
    assert comparatives[0].confidence and comparatives[0].confidence >= 0.85
    c = tables.parsed_facts
    with ENGINE.connect() as conn:
        loc = conn.execute(
            sa.select(c.c.source_loc).where(c.c.id == comparatives[0].parsed_fact_id)
        ).scalar()
    assert loc == where
    current = get_fact(GABRIEL, "profit_after_tax", Q1_26, basis, engine=ENGINE)
    assert (current.source_class.value, str(current.value)) == ("xbrl", original)
    assert not current.is_pdf_comparative


def test_a_current_column_of_the_same_pdf_is_not_a_comparative() -> None:
    """The Q1 FY27 column of that PDF is the company's report of its own period."""
    q1_27 = Period(date(2026, 4, 1), date(2026, 6, 30), PeriodType.Q)
    ours = [
        f
        for f in _all(GABRIEL, "profit_after_tax", "consolidated")
        if f.period == q1_27 and f.source_filing_id == GABRIEL_Q1_FY27_PDF
    ]
    assert [str(f.value) for f in ours] == ["1081260000.00"] and not ours[0].is_pdf_comparative
