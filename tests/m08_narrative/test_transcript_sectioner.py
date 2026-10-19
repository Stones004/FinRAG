"""Transcript sectioning on the synthetic call (M08 acceptance 12, 14, 17, 17a)."""

from __future__ import annotations

from types import ModuleType

import pytest

from finrag.narrative.models import ROLE_ANALYST, ROLE_MANAGEMENT, ROLE_MODERATOR
from finrag.narrative.patterns import load_narrative_patterns
from finrag.narrative.transcript_sectioner import (
    SECTION_CLOSING,
    SECTION_PREPARED,
    SECTION_QA,
    section_transcript,
)


@pytest.fixture
def result(fx: ModuleType):
    return section_transcript(fx.transcript_pages(), load_narrative_patterns().transcript)


def test_acceptance_12_sections(result) -> None:
    sections = [(u.section, u.speaker) for u in result.units]
    assert sections[:3] == [
        (SECTION_PREPARED, "Nina Patel"),
        (SECTION_PREPARED, "Raj Sharma"),
        (SECTION_PREPARED, "Asha Rao"),
    ]
    qa = [u.speaker for u in result.units if u.section == SECTION_QA]
    assert qa == ["Ravi Menon", "Chi Ho Wong", "Meera Iyer", "Pranav", "Ravi Menon"]
    closing = [u for u in result.units if u.section == SECTION_CLOSING]
    assert [(u.speaker, u.speaker_role) for u in closing] == [("Raj Sharma", ROLE_MANAGEMENT)]
    assert "guide for 35 percent" in closing[0].text
    assert "concludes this conference" not in " ".join(u.text for u in result.units)


def test_acceptance_12_cover_letter_and_footers_are_excluded(result) -> None:
    everything = " ".join(u.text + " ".join(u.answers) for u in result.units)
    assert "Sub: Transcript" not in everything and "Scrip Code" not in everything
    assert "Page 2 of 4" not in everything
    assert "May 04, 2026" not in everything  # repeated page header
    assert result.summary.extra["cover_pages"] == [1, 2]  # letter, then the title page


def test_prepared_remarks_do_not_start_the_qa_early(result) -> None:
    # "I now hand the conference over to Ms. Nina Patel" must not end or start anything
    first_qa = next(u for u in result.units if u.section == SECTION_QA)
    assert first_qa.start_page == 3
    assert "hand the conference" not in first_qa.text


def test_acceptance_14_roles_and_firms(result) -> None:
    by_name = {(u.section, u.speaker): u for u in result.units}
    assert by_name[(SECTION_PREPARED, "Nina Patel")].speaker_role == ROLE_MODERATOR  # host
    assert by_name[(SECTION_PREPARED, "Raj Sharma")].speaker_role == ROLE_MANAGEMENT
    qa = [u for u in result.units if u.section == SECTION_QA]
    assert {u.speaker_role for u in qa} == {ROLE_ANALYST}
    assert [u.firm for u in qa] == [
        "Alpha Capital",
        "Pictet Asset Management",  # intro "<Name>, <Firm>" (comma, no "from")
        "Beta Securities",
        "Gamma Funds",
        "Alpha Capital",
    ]


def test_answers_name_the_answerer_and_role(result) -> None:
    first = next(u for u in result.units if u.section == SECTION_QA)
    assert first.answers[0].startswith("Answer (Asha Rao, Chief Financial Officer):")
    assert any(a.startswith("Follow-up (Ravi Menon):") for a in first.answers)
    assert any(
        a.startswith("Answer (Raj Sharma, Chairman And Managing Director):") for a in first.answers
    )
    assert first.text.startswith("Our margin was flat")  # the question, without the intro sentence
    assert "first question is from the line" not in first.text


def test_acceptance_17_intro_read_by_ir_adviser_and_reintroduced_analyst(result) -> None:
    chi, meera = [u for u in result.units if u.speaker in ("Chi Ho Wong", "Meera Iyer")]
    # the adviser's own words before the intro belong to the previous analyst's unit
    assert any("No major capex is planned" in a for a in chi.answers)
    assert not any("next question is from the line" in a for a in chi.answers)
    # the lost label: the text after the intro is Meera Iyer's question and it is reported
    assert meera.speaker_role == ROLE_ANALYST and meera.text.startswith("Our capex will be routine")
    assert result.summary.extra["label_lost_after_intro"] == [{"page": 4, "analyst": "Meera Iyer"}]
    ravi = [u for u in result.units if u.speaker == "Ravi Menon"]
    assert len(ravi) == 2 and ravi[0] is not ravi[1]  # a re-introduced analyst starts a new unit


def test_unresolved_roles_and_unattributed_turns_are_reported_not_guessed(result) -> None:
    assert result.summary.extra["unattributed_turns"] == []
    assert result.summary.extra["null_role_turns"] == []


def test_acceptance_17a_bold_label_the_pattern_misses_is_attributed(fx: ModuleType) -> None:
    pages = fx.transcript_pages()
    # "van der Berg:" starts in lower case, so the text pattern misses it; the bold line saves it
    pages[3]["lines"].insert(2, fx._line("van der Berg:", bold=True))
    pages[3]["lines"].insert(3, fx._line("A comment from a person with a lower-case name."))
    out = section_transcript(pages, load_narrative_patterns().transcript)
    texts = " ".join(" ".join(u.answers) for u in out.units)
    assert "Answer (van der Berg" in texts
    assert out.summary.extra["unattributed_turns"] == []


def test_acceptance_17a_label_shaped_line_that_is_not_bold_is_unattributed(fx: ModuleType) -> None:
    pages = fx.transcript_pages()
    pages[3]["lines"].insert(2, fx._line("Mystery Person: I speak but my label has no bold."))
    out = section_transcript(pages, load_narrative_patterns().transcript)
    assert [t["page"] for t in out.summary.extra["unattributed_turns"]] == [4]
    first = next(u for u in out.units if u.section == SECTION_QA)
    assert any(a.startswith("Answer (unattributed): Mystery Person:") for a in first.answers)


def test_a_non_bold_label_of_a_listed_participant_is_attributed_and_reported(
    fx: ModuleType,
) -> None:
    pages = fx.transcript_pages()
    pages[3]["lines"].insert(2, fx._line("Vikram Seth: A reply whose label was not bold."))
    out = section_transcript(pages, load_narrative_patterns().transcript)
    assert out.summary.extra["non_bold_labels"] == [{"page": 4, "speaker": "Vikram Seth"}]
    assert out.summary.extra["unattributed_turns"] == []


def test_the_host_becomes_an_analyst_from_her_own_intro(fx: ModuleType) -> None:
    pages = fx.transcript_pages()
    pages[4]["lines"][6:6] = [  # a Moderator intro of the host, then her question (page 5)
        fx._label("Moderator"),
        fx._line("The next question is from the line of Nina Patel from Example Securities."),
        fx._label("Nina Patel"),
        fx._line("Can you comment on working capital days?"),
    ]
    out = section_transcript(pages, load_narrative_patterns().transcript)
    host_prepared = next(
        u for u in out.units if u.section == SECTION_PREPARED and u.speaker == "Nina Patel"
    )
    host_qa = next(u for u in out.units if u.section == SECTION_QA and u.speaker == "Nina Patel")
    assert host_prepared.speaker_role == ROLE_MODERATOR
    assert (host_qa.speaker_role, host_qa.firm) == (ROLE_ANALYST, "Example Securities")


def test_no_qa_marker_everything_is_prepared_remarks_with_a_warning(fx: ModuleType) -> None:
    pages = fx.transcript_pages()[:3]
    for line in pages[2]["lines"]:
        line["text"] = line["text"].replace("question is from the line of", "query from")
    out = section_transcript(pages, load_narrative_patterns().transcript)
    assert {u.section for u in out.units} == {SECTION_PREPARED}
    assert any("no Q&A start" in w for w in out.summary.warnings)


def test_no_closing_handover_means_no_closing_section_and_a_warning(fx: ModuleType) -> None:
    pages = fx.transcript_pages()
    for line in pages[4]["lines"]:
        line["text"] = line["text"].replace(
            "over to the management for the closing comments.", "over."
        )
        line["text"] = line["text"].replace("that concludes this conference", "bye")
    out = section_transcript(pages, load_narrative_patterns().transcript)
    assert not [u for u in out.units if u.section == SECTION_CLOSING]
    assert any("no closing hand-over" in w for w in out.summary.warnings)


def test_a_transcript_without_a_title_block_indexes_nothing(fx: ModuleType) -> None:
    out = section_transcript(fx.transcript_pages()[:1], load_narrative_patterns().transcript)
    assert out.units == [] and any("title block" in w for w in out.summary.warnings)
