"""Page classification: statement type, basis, unit line."""

from __future__ import annotations

import pytest

from finrag.core.config import ConfigError
from finrag.core.types import Basis
from finrag.parsing.page_classifier import (
    basis_of,
    classify_pages,
    statement_type_of,
    unit_hint_of,
)
from finrag.parsing.pdf_text import PageText
from finrag.parsing.sections import load_section_patterns

PL = """ALPHA INDUSTRIES LIMITED
Statement of Standalone Profit and Loss for the year ended March 31, 2026
(Amount in ` millions)
Revenue from operations 42,329.87
Total income 42,829.50
Profit before tax 3,219.44
Basic earnings per share 16.93"""
BS = """Consolidated Balance Sheet as at March 31, 2026
(Rs. in crore)
Total assets 21,079.30
Total equity and liabilities 21,079.30"""
CF = """Statement of Cash Flows
Cash generated from operations 3,387.32
Net cash from operating activities 2,673.80"""
BS_NO_UNIT = """Balance Sheet as at March 31, 2026
Total assets 1.00
Total equity and liabilities 1.00"""
NARRATIVE = "The company is pleased to present its annual report. The board met five times."


@pytest.fixture
def patterns():
    return load_section_patterns()


def page(number: int, text: str) -> PageText:
    return PageText(number, text, "native", None, len(text), 1.0, False)


def test_statement_types(patterns) -> None:
    assert statement_type_of(PL, patterns, 2)[0] == "profit_and_loss"
    assert statement_type_of(BS, patterns, 2)[0] == "balance_sheet"
    assert statement_type_of(CF, patterns, 2)[0] == "cash_flow"


def test_pl_page_is_classified_with_its_score(patterns) -> None:
    labels = classify_pages([page(1, PL), page(2, NARRATIVE)], patterns)
    assert (labels[0].page, labels[0].statement_type) == (1, "profit_and_loss")
    assert labels[0].score >= 2
    assert labels[1].statement_type is None


def test_pages_below_the_minimum_score_are_unlabelled(patterns) -> None:
    assert statement_type_of("Total income was higher this year.", patterns, 2) == (None, 1)


def test_a_tie_is_not_guessed(patterns) -> None:
    both = "Balance sheet\nTotal assets\nCash flows\nCash generated from operations"
    statement, score = statement_type_of(both, patterns, 2)
    assert statement is None and score == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (PL, Basis.STANDALONE),
        (BS, Basis.CONSOLIDATED),
        ("Statement of Profit and Loss\nStandalone and Consolidated results", Basis.UNKNOWN),
        ("Statement of Profit and Loss\nNo basis words here", Basis.UNKNOWN),
        ("Stand-alone Balance Sheet", Basis.STANDALONE),
    ],
)
def test_basis_words_in_the_heading(patterns, text: str, expected: Basis) -> None:
    assert basis_of(text, patterns, 15) is expected


def test_basis_word_far_down_the_page_is_ignored(patterns) -> None:
    text = "\n".join(["Statement of Profit and Loss"] + ["line"] * 20 + ["consolidated notes"])
    assert basis_of(text, patterns, 15) is Basis.UNKNOWN


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(₹ in crore)", "crore"),
        ("Rs. in Crores", "crore"),
        ("(Amount in ` millions)", "million"),
        ("(Amount in million)", "million"),
        ("(All amounts in Indian Rupees in millions, unless otherwise stated)", "million"),
        ("(₹ in Lakhs)", "lakh"),
        ("(Rs in lakh)", "lakh"),
        ("figures in thousands", "thousand"),
        ("no unit line here", None),
        ("the company sold 5 million units", None),
    ],
)
def test_unit_lines(patterns, text: str, expected: str | None) -> None:
    assert unit_hint_of(text, patterns) == expected


def test_earliest_unit_phrase_wins(patterns) -> None:
    text = "(₹ in crore)\nA footnote: amounts in lakhs elsewhere"
    assert unit_hint_of(text, patterns) == "crore"


def test_classify_pages_fills_every_field(patterns) -> None:
    label = classify_pages([page(5, BS)], patterns)[0]
    assert (label.statement_type, label.basis, label.unit_hint) == (
        "balance_sheet",
        Basis.CONSOLIDATED,
        "crore",
    )


def test_unit_key_missing_from_settings_is_a_config_error(isolated_repo) -> None:
    path = isolated_repo / "config" / "sections.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8").replace("  crore:   [", "  zillion: [", 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="zillion"):
        load_section_patterns()


def test_statement_page_without_unit_line_takes_the_earlier_statements_unit(patterns) -> None:
    labels = classify_pages(
        [page(1, NARRATIVE), page(2, PL), page(3, BS_NO_UNIT), page(4, NARRATIVE)], patterns
    )
    assert (labels[1].unit_hint, labels[1].unit_from_page) == ("million", None)
    assert (labels[2].unit_hint, labels[2].unit_from_page) == ("million", 2)
    assert labels[3].unit_hint is None  # a non-statement page never inherits


def test_no_earlier_unit_means_no_unit(patterns) -> None:
    assert classify_pages([page(1, BS_NO_UNIT)], patterns)[0].unit_hint is None
