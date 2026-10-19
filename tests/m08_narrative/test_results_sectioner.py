"""Results-PDF sectioning (M08 acceptance 1, 10) on the synthetic pages."""

from __future__ import annotations

from types import ModuleType

import pytest

from finrag.core.config import get_settings
from finrag.narrative.models import (
    BASIS_UNKNOWN,
    REASON_NOT_INDEXED,
    REASON_UNKNOWN,
    SECTION_AUDITOR,
    SECTION_NOTES,
    SECTION_PRESS,
)
from finrag.narrative.patterns import load_narrative_patterns
from finrag.narrative.runner import results_pdf_inputs
from finrag.narrative.sectioner import PageInput, section_results_pdf, spans_of


@pytest.fixture
def sectioned(fx: ModuleType):
    patterns = load_narrative_patterns()
    inputs = results_pdf_inputs(fx.results_pages(), get_settings())
    return section_results_pdf(inputs, patterns.results), inputs


def test_acceptance_1_notes_and_auditor_spans_have_the_right_pages(sectioned) -> None:
    (slices, summary), _ = sectioned
    spans = [(s.section, s.start_page, s.end_page) for s in spans_of(slices)]
    assert spans == [
        (SECTION_NOTES, 2, 3),  # heading at the bottom of the P&L page, then a numbered page
        (SECTION_NOTES, 5, 5),  # numbered notes resume after the cash-flow statement page
        (SECTION_AUDITOR, 6, 8),  # heading page, continuation, UDIN page closes the report
        (SECTION_PRESS, 10, 12),  # the news release after the annexure
    ]
    assert summary.partial_pages == [2]  # the statement part of page 2 was dropped


def test_statement_part_of_a_notes_page_is_not_indexed(sectioned) -> None:
    (slices, _), _ = sectioned
    page2 = next(s for s in slices if s.page == 2)
    assert page2.text.startswith("Notes:")
    assert "Revenue from operations" not in page2.text
    assert page2.partial


def test_statement_pages_are_skipped_and_listed(sectioned) -> None:
    (_, summary), inputs = sectioned
    skipped = {(s.start_page, s.end_page): s.reason for s in summary.skipped}
    assert skipped[(1, 1)] == REASON_UNKNOWN  # covering letter
    assert skipped[(4, 4)] == REASON_NOT_INDEXED  # cash-flow statement
    # an annexure; the press release after it (pages 10-12) is indexed, see the test below
    assert skipped[(9, 9)] == REASON_NOT_INDEXED
    assert next(s for s in summary.skipped if s.start_page == 1).detail == "To,"


def test_acceptance_10_kept_plus_skipped_equals_total(sectioned) -> None:
    (_, summary), inputs = sectioned
    assert summary.pages_total == len(inputs) == 12
    assert summary.pages_kept + summary.pages_skipped == summary.pages_total
    covered = [p for s in summary.skipped for p in range(s.start_page, s.end_page + 1)]
    assert len(covered) == len(set(covered))  # no page listed twice


def test_continuation_needs_a_notes_heading_first(fx: ModuleType) -> None:
    pages = [PageInput(1, "\n".join(["2 The figures are as stated.", "3 Segment note below."]))]
    slices, summary = section_results_pdf(pages, load_narrative_patterns().results)
    assert slices == []
    assert summary.skipped[0].reason == REASON_UNKNOWN


def test_a_statement_page_without_a_heading_ends_the_span(fx: ModuleType) -> None:
    pages = [
        PageInput(1, "Notes:\n1 First note of the results."),
        PageInput(2, "Total assets 100\nTotal equity and liabilities 100", "balance_sheet"),
        PageInput(3, "A line that follows the statement."),
    ]
    slices, summary = section_results_pdf(pages, load_narrative_patterns().results)
    assert [s.page for s in slices] == [1]
    assert [(s.start_page, s.reason) for s in summary.skipped] == [
        (2, REASON_NOT_INDEXED),
        (3, REASON_UNKNOWN),
    ]


def test_a_page_may_hold_two_headings(fx: ModuleType) -> None:
    page = PageInput(
        1,
        "Notes:\n1 The results were approved.\n"
        "Independent Auditor's Report on the results\nOpinion text",
    )
    slices, _ = section_results_pdf([page], load_narrative_patterns().results)
    assert [(s.section, s.text.splitlines()[0]) for s in slices] == [
        (SECTION_NOTES, "Notes:"),
        (SECTION_AUDITOR, "Independent Auditor's Report on the results"),
    ]


@pytest.mark.parametrize(
    "heading",
    [
        "Independent Auditor’s Limited Review Report on the Unaudited Financial Results",
        "INDEPENDENT AUDITORS' REPORT",
        "Independent Auditor�s limited review report on unaudited standalone results",
        "Review Report",
        "Other Notes",
        "Notes ;",
    ],
)
def test_real_heading_forms_are_recognised(heading: str) -> None:
    slices, _ = section_results_pdf(
        [PageInput(1, f"{heading}\nbody")], load_narrative_patterns().results
    )
    assert len(slices) == 1


def test_sentences_that_mention_the_auditor_are_not_headings() -> None:
    text = "Auditor's report with unmodified opinion. A declaration to that effect is enclosed."
    slices, _ = section_results_pdf([PageInput(1, text)], load_narrative_patterns().results)
    assert slices == []


def test_the_press_release_runs_to_the_end_and_is_its_own_section_with_unknown_basis(
    sectioned,
) -> None:
    """HDFC appends its news release (asset quality, deposits, advances): management commentary.
    Its "Note:" footnote and numbered lines are part of it, not notes to the results."""
    (slices, summary), _ = sectioned
    press = [s for s in slices if s.section == SECTION_PRESS]
    assert [s.page for s in press] == [10, 11, 12]
    assert {s.basis for s in press} == {BASIS_UNKNOWN}
    assert press[1].text.startswith("Note:")
    assert not any(s.section != SECTION_PRESS and s.page >= 10 for s in slices)
    assert {(s.start_page, s.end_page) for s in summary.skipped} == {(1, 1), (4, 4), (9, 9)}


def test_a_statement_table_inside_the_press_release_is_not_indexed() -> None:
    pages = [
        PageInput(1, "NEWS RELEASE Acme Ltd.\nThe bank grew advances by 12 percent."),
        PageInput(2, "Total assets 100\nTotal equity and liabilities 100", "balance_sheet"),
        PageInput(3, "Gross NPA stood at 1.17 percent."),
    ]
    slices, summary = section_results_pdf(pages, load_narrative_patterns().results)
    assert [(s.page, s.section) for s in slices] == [(1, SECTION_PRESS), (3, SECTION_PRESS)]
    assert [(s.start_page, s.reason) for s in summary.skipped] == [(2, REASON_NOT_INDEXED)]


def test_each_slice_takes_the_basis_of_its_page_heading_then_of_the_pages_before_it() -> None:
    """HDFC's PDF holds the standalone results, then the consolidated results."""
    pages = [
        PageInput(1, "Standalone Statement of Cash Flows\nNotes:\n1 One.", None, 0.9, "standalone"),
        PageInput(2, "2 A continuation page names no basis.\n3 Another note."),
        PageInput(
            3, "Consolidated segment results\nNotes:\n1 Group note.", None, 0.9, "consolidated"
        ),
        PageInput(4, "2 A continuation of the group notes.\n3 More."),
        PageInput(5, "Annexure 1\nList of entities"),
        PageInput(6, "Notes:\n9 After an annexure no basis is carried over."),
    ]
    slices, _ = section_results_pdf(pages, load_narrative_patterns().results)
    assert sorted({(s.page, s.basis) for s in slices}, key=lambda x: x[0]) == [
        (1, "standalone"),
        (2, "standalone"),
        (3, "consolidated"),
        (4, "consolidated"),
        (6, None),  # None = the filing's own basis is used
    ]
