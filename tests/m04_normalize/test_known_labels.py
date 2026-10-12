"""known_unmapped_labels: PDF/OCR labels that are understood and deliberately not mapped."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from finrag.normalize.mapper import Known, Mapped, Mapper, Unmapped
from finrag.normalize.taxonomy import TaxonomyError, parse_taxonomy
from finrag.parsing.models import ParsedFact

REAL = Path(__file__).resolve().parents[2] / "config" / "taxonomy.yaml"


def pdf(label: str, statement: str | None) -> ParsedFact:
    return ParsedFact(
        Decimal(1), label_raw=label, extractor="ocr", scale=1_000_000, statement_type=statement
    )


def synthetic(kit, **labels):
    data = kit.taxonomy_data()
    data["known_unmapped_labels"] = labels
    return data


def test_known_label_is_scoped_by_statement(kit) -> None:
    data = synthetic(kit, cash_flow={"interest paid": "cash_flow_adjustment"})
    mapper = Mapper(parse_taxonomy(data), fuzzy_min_score=85)
    known = mapper.map_fact(pdf("Interest paid", "cash_flow"), "", "general")
    assert isinstance(known, Known) and known.reason == "cash_flow_adjustment"
    assert known.entry.qname == "interest paid"
    other = mapper.map_fact(pdf("Interest paid", "profit_and_loss"), "", "general")
    assert isinstance(other, Unmapped)


def test_a_concept_label_still_wins_over_the_known_list(kit) -> None:
    mapper = Mapper(parse_taxonomy(synthetic(kit, cash_flow={"x": "ratio"})), fuzzy_min_score=85)
    assert mapper.map_fact(pdf("Revenue from operations", "profit_and_loss"), "", "general") == (
        Mapped("revenue", "label")
    )


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        ({"cash_flow": {"Interest Paid": "ratio"}}, "not in normalised form"),
        ({"nowhere": {"interest paid": "ratio"}}, "unknown statement 'nowhere'"),
        ({"cash_flow": {"interest paid": "because"}}, "unknown reason 'because'"),
        ({"profit_and_loss": {"revenue from operations": "ratio"}}, "also a label of 'revenue'"),
    ],
)
def test_invalid_known_labels_fail_with_a_message(kit, labels, message) -> None:
    with pytest.raises(TaxonomyError, match=message):
        parse_taxonomy(synthetic(kit, **labels))


# --- the real registry -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real():
    return parse_taxonomy(yaml.safe_load(REAL.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def real_mapper(real):
    return Mapper(real, fuzzy_min_score=85)


@pytest.mark.parametrize(
    ("label", "statement", "reason"),
    [
        ("Total non-current assets", "balance_sheet", "subtotal"),
        ("Total non-current liabilities", "balance_sheet", "subtotal"),
        ("Cash generated from operating activities", "cash_flow", "subtotal"),
        ("Depreciation and amortization expense", "cash_flow", "cash_flow_adjustment"),
        ("Paid up equity share capital", "profit_and_loss", "share_data"),
        ("Decrease in trade receivables", "cash_flow", "cash_flow_adjustment"),
    ],
)
def test_wrong_high_score_suggestions_are_never_mapped(real_mapper, label, statement, reason):
    result = real_mapper.map_fact(pdf(label, statement), "", "general")
    assert isinstance(result, Known) and result.reason == reason


def test_the_same_labels_are_not_known_on_the_wrong_statement(real_mapper) -> None:
    # the P&L line is a real concept; the cash-flow add-back of the same name is not that line
    assert real_mapper.map_fact(
        pdf("Depreciation and amortisation expenses", "profit_and_loss"), "", "general"
    ) == (Mapped("depreciation_amortisation", "label"))


def test_real_registry_banking_related_party_amount_is_known(real) -> None:
    entry = real.known[
        ("banking", "in-capmkt:AmountOfRelatedPartyTransactionDuringTheReportingPeriod")
    ]
    assert entry.reason == "related_party"
