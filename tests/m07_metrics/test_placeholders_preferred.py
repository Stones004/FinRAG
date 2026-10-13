"""Conditional placeholder zeros (normalizer + current view) and preferred bases."""

from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml

from finrag.core.db import tables, transaction
from finrag.facts.current_view import get_fact, get_facts
from finrag.metrics.engine import run_metrics
from finrag.metrics.query import get_metric
from finrag.metrics.registry import MetricsError, load_metrics, parse_registry
from finrag.normalize.models import CanonicalCandidate
from finrag.normalize.runner import NormalizeOutcome, _drop_guarded_zeros
from finrag.normalize.taxonomy import TaxonomyError, parse_taxonomy, placeholder_guards

D = Decimal
REAL = Path(__file__).resolve().parents[2]
FY26 = ("2025-04-01", "2026-03-31", "FY")
H1 = ("2025-04-01", "2025-09-30", "H")
H2 = ("2025-10-01", "2026-03-31", "H")


@pytest.fixture(scope="module")
def raw_taxonomy():
    return yaml.safe_load((REAL / "config" / "taxonomy.yaml").read_text("utf-8"))


def put(kit, concept: str, span: tuple[str, str, str], value, **kw) -> str:
    """Write a fact for a (start, end, type) span; value in millions."""
    return kit.put(concept, span[0], span[1], value, span[2], **kw)


def cand(concept: str, value: str, basis: str = "consolidated") -> CanonicalCandidate:
    return CanonicalCandidate(
        isin="INE111A01010",
        filing_id="FIL-x",
        parsed_fact_id=1,
        concept=concept,
        period_start=date(2025, 4, 1),
        period_end=date(2025, 9, 30),
        period_type="H",
        basis=basis,
        value=D(value),
        unit="INR",
        mapping_method="standard",
        extractor="xbrl",
        ocr_backend=None,
        confidence=1.0,
        filed_on="2026-05-01",
    )


# --- taxonomy and normalizer ---------------------------------------------------------------------


def test_owners_profit_is_a_conditional_placeholder(raw_taxonomy) -> None:
    taxonomy = parse_taxonomy(raw_taxonomy)
    owners = taxonomy.concepts["profit_attributable_owners"]
    assert owners.zero_is_placeholder_while_nonzero == "profit_after_tax"
    assert not owners.zero_is_placeholder  # a genuine zero is still possible
    assert placeholder_guards() == {"profit_attributable_owners": "profit_after_tax"}


def test_an_unknown_guard_is_a_load_error(raw_taxonomy) -> None:
    data = copy.deepcopy(raw_taxonomy)
    data["concepts"]["profit_attributable_owners"]["zero_is_placeholder_while_nonzero"] = "nope"
    with pytest.raises(TaxonomyError, match="zero_is_placeholder_while_nonzero 'nope'"):
        parse_taxonomy(data)


def test_zero_beside_nonzero_profit_is_dropped_and_counted(raw_taxonomy) -> None:
    out = NormalizeOutcome()
    out.candidates = [cand("profit_attributable_owners", "0"), cand("profit_after_tax", "110")]
    _drop_guarded_zeros(out, parse_taxonomy(raw_taxonomy))
    assert [c.concept for c in out.candidates] == ["profit_after_tax"]
    assert out.placeholder_zero == 1


@pytest.mark.parametrize(
    ("owners", "pat", "owners_basis"),
    [("0", "0", "consolidated"), ("5", "110", "consolidated"), ("0", "110", "standalone")],
)
def test_a_genuine_zero_or_other_basis_is_kept(raw_taxonomy, owners, pat, owners_basis) -> None:
    out = NormalizeOutcome()
    out.candidates = [
        cand("profit_attributable_owners", owners, owners_basis),
        cand("profit_after_tax", pat),
    ]
    _drop_guarded_zeros(out, parse_taxonomy(raw_taxonomy))
    assert len(out.candidates) == 2 and out.placeholder_zero == 0


# --- the current view hides what was stored before the rule ------------------------------------


def test_stored_placeholder_zero_and_facts_derived_from_it_are_hidden(kit) -> None:
    zero = put(kit, "profit_attributable_owners", H1, 0)
    put(kit, "profit_after_tax", H1, 1310)
    fy = put(kit, "profit_attributable_owners", FY26, 2522)
    put(kit, "profit_after_tax", FY26, 2521)
    derived = put(kit, "profit_attributable_owners", H2, 2522, derived=True)
    with transaction() as conn:
        conn.execute(
            sa.update(tables.canonical_facts)
            .where(tables.canonical_facts.c.fact_id == derived)
            .values(derivation_json=json.dumps({"input_fact_ids": [fy, zero], "all_xbrl": True}))
        )
    seen = get_facts(kit.ISIN, ["profit_attributable_owners"], None, "consolidated")
    assert [(f.period.type.value, f.value) for f in seen] == [("FY", D(2522) * 1_000_000)]
    assert get_fact(kit.ISIN, "profit_attributable_owners", kit.period(*H1), "consolidated") is None
    assert get_fact(kit.ISIN, "profit_attributable_owners", kit.period(*H2), "consolidated") is None
    with transaction() as conn:  # append-only: all three rows are still stored
        stored = conn.execute(
            sa.select(sa.func.count())
            .select_from(tables.canonical_facts)
            .where(tables.canonical_facts.c.concept == "profit_attributable_owners")
        ).scalar()
    assert stored == 3


def test_a_genuine_stored_zero_stays_visible(kit) -> None:
    put(kit, "profit_attributable_owners", H1, 0)
    put(kit, "profit_after_tax", H1, 0)
    found = get_fact(kit.ISIN, "profit_attributable_owners", kit.period(*H1), "consolidated")
    assert found is not None and found.value == 0


def test_a_revision_with_a_real_value_replaces_the_hidden_zero(kit) -> None:
    put(kit, "profit_attributable_owners", H1, 0)
    put(kit, "profit_after_tax", H1, 1310)
    put(kit, "profit_attributable_owners", H1, 1309, filing="FIL-rev", filed_on="2026-07-01")
    found = get_fact(kit.ISIN, "profit_attributable_owners", kit.period(*H1), "consolidated")
    assert found.value == D(1309) * 1_000_000


def test_owners_profit_metric_uses_total_profit_when_the_stored_zero_is_hidden(kit) -> None:
    put(kit, "profit_attributable_owners", H1, 0)
    put(kit, "profit_after_tax", H1, 1310)
    kit.put("non_controlling_interest", "2025-09-30", "2025-09-30", 0)
    run_metrics(kit.ISIN)
    got = get_metric(kit.ISIN, "profit_owners", kit.period(*H1), "consolidated")
    assert got.value == D(1310) * 1_000_000
    assert got.details["fallbacks"][0]["used"] == "profit_after_tax"


# --- preferred basis -----------------------------------------------------------------------

STANDALONE_FIRST = (
    "nii",
    "cost_to_income",
    "credit_cost",
    "credit_deposit_ratio",
    "advances_growth_yoy",
    "deposits_growth_yoy",
    "gross_npa",
    "net_npa",
    "gross_npa_ratio",
    "net_npa_ratio",
    "cet1_ratio",
)


def test_bank_operating_metrics_prefer_standalone() -> None:
    registry = load_metrics()
    for metric_id in STANDALONE_FIRST:
        assert registry.get(metric_id).preferred_basis == "standalone", metric_id
        assert registry.describe(metric_id)["preferred_basis"] == "standalone"
    for metric_id in registry.order():
        if metric_id not in STANDALONE_FIRST:
            assert registry.get(metric_id).preferred_basis is None, metric_id
    assert registry.get("roe_bank").preferred_basis is None  # group-level returns: consolidated
    assert registry.get("pat_growth_yoy").preferred_basis is None


def test_an_invalid_preferred_basis_is_refused(raw_taxonomy) -> None:
    raw = yaml.safe_load((REAL / "config" / "metrics.yaml").read_text("utf-8"))
    raw["metrics"]["nii"]["preferred_basis"] = "group"
    with pytest.raises(MetricsError, match="preferred_basis"):
        parse_registry(raw, parse_taxonomy(raw_taxonomy))


def test_both_bases_are_still_computed(kit) -> None:
    kit.sector("bank")
    kit.load_bank()
    for concept, value in (
        ("bank_interest_earned", 250),
        ("bank_interest_expended", 100),
        ("other_income", 40),
        ("bank_operating_expenses", 60),
    ):
        put(kit, concept, FY26, value, basis="standalone")
    run_metrics(kit.ISIN)
    period = kit.period(*FY26)
    cons = get_metric(kit.ISIN, "cost_to_income", period, "consolidated")
    stand = get_metric(kit.ISIN, "cost_to_income", period, "standalone")
    assert cons.value == D(90) / D(240) and stand.value == D(60) / D(190)
