"""Checks against the real parsed filings in data/finrag.db (skipped when there are none).

The real database is opened read-only; nothing is written and no review items are queued.
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
from finrag.normalize.mapper import Mapper
from finrag.normalize.runner import (
    NormalizeOutcome,
    _context,
    entity_sector,
    load_rows,
    normalize_facts,
)
from finrag.normalize.taxonomy import Taxonomy, parse_taxonomy

REAL = Path(__file__).resolve().parents[2]
DB = REAL / "data" / "finrag.db"
GOLDEN = REAL / "tests" / "golden" / "golden_values.yaml"
MIN_CONFIDENCE = 0.7
NETWEB, GABRIEL, HDFC = "INE0NT901020", "INE524A01029", "INE040A01034"
ROUNDING = Decimal(10000)  # the XBRL files round to ten thousand rupees at most


def _engine() -> Engine | None:
    """Read-only engine on the real database, or None when it has no parsed facts."""
    if not DB.exists():
        return None
    engine = sa.create_engine(f"sqlite:///file:{DB.as_posix()}?mode=ro&uri=true")
    with engine.connect() as conn:
        try:
            found = conn.execute(
                sa.select(sa.func.count()).select_from(tables.parsed_facts)
            ).scalar()
        except sa.exc.OperationalError:
            return None
    return engine if found else None


ENGINE = _engine()
pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(ENGINE is None, reason="no parsed real filings"),
]


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return parse_taxonomy(yaml.safe_load((REAL / "config" / "taxonomy.yaml").read_text("utf-8")))


@pytest.fixture(scope="module")
def mapper(taxonomy: Taxonomy) -> Mapper:
    return Mapper(taxonomy, fuzzy_min_score=85)


@pytest.fixture(scope="module")
def outcomes(mapper: Mapper) -> dict[str, tuple[sa.Row, NormalizeOutcome]]:
    """filing_id -> (filing row, outcome) for every parsed filing that has facts."""
    assert ENGINE is not None
    with ENGINE.connect() as conn:
        filings = conn.execute(
            sa.select(tables.filings).where(tables.filings.c.status.in_(["parsed", "normalized"]))
        ).all()
    result = {}
    for filing in filings:
        rows = load_rows(filing.filing_id, ENGINE)
        if not rows:
            continue
        ctx = _context(filing, entity_sector(filing.isin, ENGINE), mapper.taxonomy)
        result[filing.filing_id] = (
            filing,
            normalize_facts(rows, ctx, mapper, MIN_CONFIDENCE),
        )
    return result


def _golden() -> list[tuple[str, dict]]:
    if not GOLDEN.exists():
        return []
    data = yaml.safe_load(GOLDEN.read_text(encoding="utf-8")) or {}
    return [(isin, e) for isin, entries in data.items() for e in entries]


ENTRIES = _golden()


def _family(isin: str) -> str:
    return "banking" if isin == HDFC else "indas"


@pytest.mark.parametrize(
    ("isin", "entry"),
    ENTRIES,
    ids=[f"{i[-4:]}-{e['concept']}-{e['period']}-{e['source_file'][-24:]}" for i, e in ENTRIES],
)
def test_golden_qnames_map_to_the_golden_concept(taxonomy, isin: str, entry: dict) -> None:
    assert taxonomy.by_qname[(_family(isin), entry["xbrl_concept"])] == entry["concept"]


def _candidate(outcomes, isin: str, entry: dict):
    """The candidate for a golden entry from the highest revision of its filing."""
    start, end = entry["period_dates"][0], entry["period_dates"][-1]
    concept = entry["concept"]
    best = None
    for filing, outcome in outcomes.values():
        if filing.isin != isin or filing.format != "xbrl" or filing.basis != entry["basis"]:
            continue
        if date.fromisoformat(filing.period_end) != end:
            continue
        for cand in outcome.candidates:
            if cand.concept == concept and cand.period_start == start and cand.period_end == end:
                if best is None or filing.revision_no > best[0]:
                    best = (filing.revision_no, cand)
    return None if best is None else best[1]


VERIFIED = [(i, e) for i, e in ENTRIES if e.get("verified") is True]


@pytest.mark.parametrize(
    ("isin", "entry"), VERIFIED, ids=[f"{i[-4:]}-{e['concept']}-{e['period']}" for i, e in VERIFIED]
)
def test_verified_golden_values_survive_normalization(outcomes, isin: str, entry: dict) -> None:
    candidate = _candidate(outcomes, isin, entry)
    assert candidate is not None, "no candidate for this golden entry"
    assert candidate.value == Decimal(entry["value_inr"])
    assert candidate.mapping_method == "standard" and candidate.extractor == "xbrl"


def test_every_unverified_golden_entry_also_normalizes_to_its_value(outcomes) -> None:
    mismatches = []
    for isin, entry in ENTRIES:
        candidate = _candidate(outcomes, isin, entry)
        if candidate is None or candidate.value != Decimal(entry["value_inr"]):
            mismatches.append((isin, entry["concept"], entry["period"]))
    assert not mismatches


def test_no_real_xbrl_fact_is_left_unmapped_or_unnormalizable(outcomes) -> None:
    """Every entity-total XBRL fact is mapped or known-unmapped; none raises an issue."""
    xbrl = [(f, o) for f, o in outcomes.values() if f.format == "xbrl"]
    assert xbrl
    for filing, outcome in xbrl:
        assert [u.outcome.key for u in outcome.unmapped] == [], filing.filing_id
        assert [(i.reason, i.key) for i in outcome.issues] == [], filing.filing_id
        assert outcome.basis_unknown == 0
        mapped = sum(outcome.mapped_by_method.values())
        known = sum(outcome.known_by_reason.values())
        total = mapped + known + outcome.not_applicable + outcome.placeholder_zero
        assert total == outcome.considered, filing.filing_id


def test_bank_segment_revenue_is_never_revenue(outcomes) -> None:
    bank = [o for f, o in outcomes.values() if f.isin == HDFC and f.format == "xbrl"]
    assert bank
    for outcome in bank:
        assert outcome.known_by_reason["segment"] > 0
        assert not any(c.concept == "revenue" for c in outcome.candidates)


def test_opening_cash_equals_the_prior_closing_cash(outcomes) -> None:
    """The check M06 will make: opening cash of FY26 is closing cash at the end of FY25."""
    cash: dict[tuple[str, date], Decimal] = {}
    for filing, outcome in outcomes.values():
        if filing.isin != NETWEB or filing.format != "xbrl":
            continue
        for cand in outcome.candidates:
            if cand.concept in ("opening_cash", "closing_cash"):
                cash[(cand.concept, cand.period_end)] = cand.value
    assert cash[("opening_cash", date(2025, 3, 31))] == cash[("closing_cash", date(2025, 3, 31))]
    assert cash[("opening_cash", date(2025, 3, 31))] == Decimal("1700830000")
    assert cash[("closing_cash", date(2026, 3, 31))] == Decimal("3655220000")


def test_netweb_scan_agrees_with_the_xbrl_revision(outcomes) -> None:
    """Non-circular: OCR candidates (label-mapped) match the XBRL revision within rounding."""
    xbrl = {
        c.key: c.value
        for f, o in outcomes.values()
        if f.isin == NETWEB and f.format == "xbrl" and f.revision_no == 1
        and f.period_end == "2026-03-31"
        for c in o.candidates
    }  # fmt: skip
    scan = [
        c
        for f, o in outcomes.values()
        if f.isin == NETWEB and f.format == "pdf" and f.period_end == "2026-03-31"
        for c in o.candidates
    ]
    assert scan
    checked = {}
    for cand in scan:
        if cand.confidence >= MIN_CONFIDENCE and cand.key in xbrl:
            assert abs(cand.value - xbrl[cand.key]) <= ROUNDING, cand.key
            assert cand.extractor == "ocr" and cand.mapping_method == "label"
            checked[cand.concept] = cand.value
    for concept in (
        "revenue",
        "profit_after_tax",
        "cfo",
        "closing_cash",
        "opening_cash",
        "dividends_paid",
    ):
        assert concept in checked, concept


def test_netweb_dividends_paid_has_the_xbrl_sign_after_conversion(outcomes) -> None:
    """The PDF prints -141.63 mn; the XBRL tags +141.63 mn. After pdf_sign they are equal."""
    got = {
        (c.extractor, c.period_end): c.value
        for f, o in outcomes.values()
        if f.isin == NETWEB
        for c in o.candidates
        if c.concept == "dividends_paid" and c.period_end == date(2026, 3, 31)
    }
    assert got[("xbrl", date(2026, 3, 31))] == Decimal("141630000")
    assert got[("ocr", date(2026, 3, 31))] == Decimal("141630000.00")


def test_label_statement_filter_resolves_finance_costs(outcomes) -> None:
    """P&L 'Finance costs' maps; the cash-flow adjustment of the same name does not."""
    scan = next(o for f, o in outcomes.values() if f.filing_id == "FIL-84bf2a7b31af")
    finance = [c for c in scan.candidates if c.concept == "finance_cost"]
    assert {c.value for c in finance if c.period_end == date(2026, 3, 31)} >= {
        Decimal("129530000.00")
    }
    assert not any(i.reason == "duplicate_candidates" for i in scan.issues)


def test_hdfc_npa_zero_is_a_placeholder_and_the_standalone_value_is_stored(outcomes) -> None:
    """Consolidated filings tag 0 for NPA and CET1; only the standalone filing has real values."""
    consolidated = [
        o
        for f, o in outcomes.values()
        if f.isin == HDFC and f.format == "xbrl" and f.basis == "consolidated"
    ]
    standalone = [
        o
        for f, o in outcomes.values()
        if f.isin == HDFC and f.format == "xbrl" and f.basis == "standalone"
    ]
    assert consolidated and standalone
    bank = (
        "bank_gross_npa",
        "bank_net_npa",
        "bank_gross_npa_ratio",
        "bank_net_npa_ratio",
        "bank_cet1_ratio",
    )
    for outcome in consolidated:
        assert outcome.placeholder_zero > 0
        assert not any(c.concept in bank for c in outcome.candidates)
    got = {c.concept: c.value for o in standalone for c in o.candidates if c.concept in bank}
    assert got == {
        "bank_gross_npa": Decimal("358463500000"),
        "bank_net_npa": Decimal("123572700000"),
        "bank_gross_npa_ratio": Decimal("0.0117"),
        "bank_net_npa_ratio": Decimal("0.0041"),
        "bank_cet1_ratio": Decimal("0.1957"),
    }


def test_hdfc_operating_profit_before_provisions_is_mapped(outcomes) -> None:
    got = {
        (c.period_type, c.period_end): c.value
        for f, o in outcomes.values()
        if f.filing_id == "FIL-de471a6c3e9a"
        for c in o.candidates
        if c.concept == "bank_operating_profit_before_provisions"
    }
    assert got[("Q", date(2026, 3, 31))] == Decimal("311116800000")
    assert got[("FY", date(2026, 3, 31))] == Decimal("1287976700000")
