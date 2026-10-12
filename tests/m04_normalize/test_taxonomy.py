"""Taxonomy loading, validation and label normalisation."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from finrag.normalize.taxonomy import TaxonomyError, normalise_label, parse_taxonomy

REAL = Path(__file__).resolve().parents[2] / "config" / "taxonomy.yaml"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Revenue from Operations", "revenue from operations"),
        ("  Revenue   from\toperations ", "revenue from operations"),
        ("(a) Property, plant and equipment", "property plant and equipment"),
        ("1. Total income", "total income"),
        ("Revenue from operations (Refer note 3)", "revenue from operations"),
        ("Profit & Loss", "profit and loss"),
        ("ix Total comprehensive income", "total comprehensive income"),
        ("ll Profit before tax", "profit before tax"),
        ("Total expenses", "total expenses"),
    ],
)
def test_normalise_label(label: str, expected: str) -> None:
    assert normalise_label(label) == expected


def test_synthetic_taxonomy_indexes(taxonomy) -> None:
    assert taxonomy.by_qname[("indas", "in-capmkt:RevenueFromOperations")] == "revenue"
    assert taxonomy.by_qname[("banking", "in-capmkt:ProfitLossForThePeriod")] == "profit_after_tax"
    assert taxonomy.by_label["net sales"] == "revenue"
    assert taxonomy.known[("banking", "in-capmkt:SegmentRevenue")].reason == "segment"
    assert taxonomy.opening_names == {"opening_cash"}


def test_lookup_is_keyed_by_family(taxonomy) -> None:
    assert ("banking", "in-capmkt:RevenueFromOperations") not in taxonomy.by_qname


def test_duplicate_qname_in_one_family_fails_with_message(kit) -> None:
    data = kit.taxonomy_data()
    data["concepts"]["cfo"]["xbrl_concepts"]["indas"].append("in-capmkt:RevenueFromOperations")
    with pytest.raises(
        TaxonomyError, match="duplicate mapping.*RevenueFromOperations.*revenue.*cfo"
    ):
        parse_taxonomy(data)


def test_same_qname_in_two_families_is_allowed(taxonomy) -> None:
    assert taxonomy.by_qname[("indas", "in-capmkt:Assets")] == "total_assets"
    assert taxonomy.by_qname[("banking", "in-capmkt:Assets")] == "total_assets"


def test_qname_both_mapped_and_known_unmapped_fails(kit) -> None:
    data = kit.taxonomy_data()
    data["known_unmapped"]["indas"]["in-capmkt:RevenueFromOperations"] = "ratio"
    with pytest.raises(TaxonomyError, match="known_unmapped and mapped"):
        parse_taxonomy(data)


def test_unknown_reason_fails(kit) -> None:
    data = kit.taxonomy_data()
    data["known_unmapped"]["indas"]["in-capmkt:Whatever"] = "because"
    with pytest.raises(TaxonomyError, match="unknown reason 'because'"):
        parse_taxonomy(data)


def test_duplicate_of_unknown_concept_fails(kit) -> None:
    data = kit.taxonomy_data()
    data["known_unmapped"]["indas"]["in-capmkt:Whatever"] = "duplicate_of:nope"
    with pytest.raises(TaxonomyError, match="duplicate_of unknown concept 'nope'"):
        parse_taxonomy(data)


def test_duplicate_label_across_concepts_fails(kit) -> None:
    data = kit.taxonomy_data()
    data["concepts"]["cfo"]["labels"].append("REVENUE from operations")
    with pytest.raises(TaxonomyError, match="duplicate label"):
        parse_taxonomy(data)


def test_unknown_family_and_bad_qname_fail(kit) -> None:
    data = kit.taxonomy_data()
    data["concepts"]["cfo"]["xbrl_concepts"]["other"] = ["in-capmkt:X"]
    with pytest.raises(TaxonomyError, match="unknown taxonomy family 'other'"):
        parse_taxonomy(data)
    data = kit.taxonomy_data()
    data["concepts"]["cfo"]["xbrl_concepts"]["indas"] = ["NotAQName"]
    with pytest.raises(TaxonomyError, match="not a prefix:LocalName QName"):
        parse_taxonomy(data)


def test_unknown_opening_concept_fails(kit) -> None:
    data = kit.taxonomy_data()
    data["concepts"]["closing_cash"]["opening_concept"] = "nope"
    with pytest.raises(TaxonomyError, match="opening_concept 'nope' unknown"):
        parse_taxonomy(data)


def test_only_as_reported_sign_is_accepted(kit) -> None:
    data = kit.taxonomy_data()
    data["concepts"]["cfo"]["sign"] = "negative"
    with pytest.raises(TaxonomyError, match="sign"):
        parse_taxonomy(data)


def test_unknown_field_is_rejected(kit) -> None:
    data = copy.deepcopy(kit.taxonomy_data())
    data["concepts"]["cfo"]["colour"] = "red"
    with pytest.raises(TaxonomyError, match="colour"):
        parse_taxonomy(data)


# --- the real registry -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real():
    return parse_taxonomy(yaml.safe_load(REAL.read_text(encoding="utf-8")))


def test_real_taxonomy_loads(real) -> None:
    assert real.families == ("indas", "banking")
    assert real.concepts["capex_ppe"].sign_convention == "outflow_positive"
    assert real.concepts["dividends_paid"].sign_convention == "outflow_positive"
    assert real.concepts["revenue"].sign_convention == "natural"


def test_real_taxonomy_decisions(real) -> None:
    ocr = real.known[("indas", "in-capmkt:OtherComprehensiveIncomeNetOfTaxes")]
    assert ocr.duplicate_of == "other_comprehensive_income"
    segment = real.known[("banking", "in-capmkt:SegmentRevenue")]
    assert segment.reason == "segment" and "never be treated as revenue" in segment.note
    assert (
        real.by_qname[("indas", "in-capmkt:TradeReceivablesCurrent")] == "trade_receivables_current"
    )
    assert (
        real.by_qname[("indas", "in-capmkt:TradePayablesNoncurrent")] == "trade_payables_noncurrent"
    )
    assert real.concepts["closing_cash"].opening_concept == "opening_cash"
    assert "current maturities" in real.concepts["short_term_borrowings"].description
    assert not real.concepts["lease_liabilities"].xbrl_concepts  # no tag in any real filing
    for concept in real.concepts.values():
        assert concept.sign == "as_reported"


def test_real_taxonomy_only_has_bank_concepts_for_banks(real) -> None:
    for name, concept in real.concepts.items():
        if name.startswith("bank_"):
            assert [s.value for s in concept.sectors or []] == ["bank"]
            assert set(concept.xbrl_concepts) <= {"banking"}
