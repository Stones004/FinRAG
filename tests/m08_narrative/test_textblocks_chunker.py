"""XBRL text blocks and the chunker (M08 acceptance 2, 3, 9, 13)."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

from finrag.core.config import get_settings
from finrag.narrative.chunker import (
    chunk_results_slices,
    chunk_text,
    chunk_text_block,
    chunk_transcript_unit,
    count_tokens,
    repeated_edge_lines,
)
from finrag.narrative.models import PageSlice, TranscriptUnit
from finrag.narrative.patterns import load_narrative_patterns
from finrag.narrative.textblocks import clean_block, read_text_blocks


@pytest.fixture
def narrative_settings():
    return get_settings().narrative


def _write(path: Path, facts: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(f) for f in facts), encoding="utf-8")
    return path


def test_acceptance_9_exactly_the_four_concepts_become_blocks(
    fx: ModuleType, tmp_path: Path
) -> None:
    blocks = read_text_blocks(
        _write(tmp_path / "t.jsonl", fx.text_facts()), load_narrative_patterns().text_blocks
    )
    assert [(b.section, b.concept.split(":")[1]) for b in blocks] == [
        ("results_notes", "DisclosureOfNotesOnFinancialResultsExplanatoryTextBlock"),
        ("segment_notes", "DisclosureOfNotesOnSegmentsExplanatoryTextBlock"),
        ("assets_liabilities_notes", "DisclosureOfNotesOnAssetsAndLiabilitiesExplanatoryTextBlock"),
        (
            "audit_opinion",
            "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification",
        ),
    ]
    # metadata facts, the dimensioned segment block and a related-party fact are all ignored
    assert "ignored" not in " ".join(b.text for b in blocks)
    assert "Related Party" not in " ".join(b.text for b in blocks)


def test_missing_text_facts_file_gives_no_blocks(tmp_path: Path) -> None:
    assert read_text_blocks(tmp_path / "none.jsonl", load_narrative_patterns().text_blocks) == []


def test_html_breaks_become_line_breaks() -> None:
    assert clean_block("1. One. <BR> \n2. Two &amp; more") == "1. One.\n2. Two & more"


def test_acceptance_9_the_audit_declaration_is_kept_whole_despite_its_size(
    fx: ModuleType, narrative_settings, tmp_path: Path
) -> None:
    blocks = read_text_blocks(
        _write(tmp_path / "t.jsonl", fx.text_facts()), load_narrative_patterns().text_blocks
    )
    declaration = next(b for b in blocks if b.section == "audit_opinion")
    chunks = chunk_text_block(declaration, narrative_settings, {"basis": "standalone"})
    assert len(chunks) == 1 and chunks[0].text == "Unmodified opinion"
    assert chunks[0].token_count < narrative_settings.min_chunk_tokens


def test_acceptance_2_text_block_chunks_have_no_page_and_name_their_concept(
    fx: ModuleType, narrative_settings, tmp_path: Path
) -> None:
    blocks = read_text_blocks(
        _write(tmp_path / "t.jsonl", fx.text_facts()), load_narrative_patterns().text_blocks
    )
    for block in blocks:
        for chunk in chunk_text_block(block, narrative_settings, {}):
            assert chunk.page is None and chunk.meta["concept"] == block.concept


def test_long_text_is_split_with_overlap_and_no_sentence_is_lost(narrative_settings) -> None:
    sentences = [
        f"Sentence number {n} talks about the inventory valuation change." for n in range(120)
    ]
    chunks = chunk_text(" ".join(sentences), 3, narrative_settings)
    assert len(chunks) > 1
    assert all(tokens <= narrative_settings.chunk_tokens for _, tokens in chunks[:-1])
    joined = " ".join(text for text, _ in chunks)
    assert all(s in joined for s in sentences)
    # overlap: the last sentence of a chunk opens the next one
    last_of_first = chunks[0][0].rsplit(". ", 1)[-1]
    assert last_of_first.strip(". ") in chunks[1][0]


def test_a_tiny_remainder_is_merged_not_dropped(narrative_settings) -> None:
    body = " ".join(
        f"Sentence {n} about margins and costs of the business unit." for n in range(34)
    )
    chunks = chunk_text(body + " The end.", None, narrative_settings)
    assert chunks[-1][0].endswith("The end.")
    assert all(tokens >= narrative_settings.min_chunk_tokens for _, tokens in chunks)


def test_acceptance_3_repeated_page_headers_and_footers_are_stripped(narrative_settings) -> None:
    topics = ["margins", "inventory", "tax", "dividends", "segments", "leases", "debt", "capex"]
    pages = []
    for n in range(1, 8):
        body = [f"line {i} of page {n} about {topics[(n + i) % 8]} in the notes" for i in range(8)]
        pages.append("\n".join(["ACME WIDGETS LIMITED", *body, f"Page {n} of 9"]))
    furniture = repeated_edge_lines(pages)
    slices = [PageSlice(n, "results_notes", text) for n, text in enumerate(pages, start=1)]
    chunks = chunk_results_slices(slices, furniture, narrative_settings, {})
    joined = " ".join(c.text for c in chunks)
    assert "ACME WIDGETS" not in joined and "Page 3 of 9" not in joined
    assert "line 3 of page 4 about" in joined


def test_acceptance_2_pdf_chunks_have_the_page_where_they_start(narrative_settings) -> None:
    slices = [
        PageSlice(5, "results_notes", "Notes:\nFirst note on page five."),
        PageSlice(6, "results_notes", "Second note on page six."),
        PageSlice(9, "auditor_report", "Independent Auditor's Report\nOpinion."),
    ]
    chunks = chunk_results_slices(slices, set(), narrative_settings, {"basis": "standalone"})
    assert [(c.section, c.page, c.meta["end_page"]) for c in chunks] == [
        ("results_notes", 5, 6),
        ("auditor_report", 9, 9),  # a chunk never crosses a section boundary
    ]


def _qa_unit(answer_words: int) -> TranscriptUnit:
    answer = " ".join(
        f"The answer sentence number {n} explains the margin." for n in range(answer_words)
    )
    return TranscriptUnit(
        "qa",
        "Ravi Menon",
        "analyst",
        "Alpha Capital",
        3,
        5,
        "What drove the gross margin this quarter?",
        (
            f"Answer (Asha Rao, Chief Financial Officer): {answer}",
            "Follow-up (Ravi Menon): And next year?",
        ),
    )


def test_acceptance_13_a_question_stays_with_its_answer(narrative_settings) -> None:
    chunks = chunk_transcript_unit(_qa_unit(4), narrative_settings, {})
    assert len(chunks) == 1
    assert chunks[0].text.startswith("Question (Ravi Menon, Alpha Capital): What drove")
    assert "Answer (Asha Rao, Chief Financial Officer):" in chunks[0].text
    assert (chunks[0].speaker, chunks[0].speaker_role, chunks[0].page) == (
        "Ravi Menon",
        "analyst",
        3,
    )
    assert chunks[0].meta["firm"] == "Alpha Capital"


def test_acceptance_13_a_long_answer_repeats_the_question_in_every_chunk(
    narrative_settings,
) -> None:
    chunks = chunk_transcript_unit(_qa_unit(200), narrative_settings, {})
    assert len(chunks) > 2
    for chunk in chunks:
        assert chunk.text.startswith("Question (Ravi Menon, Alpha Capital): What drove")
        assert "Answer (" in chunk.text  # no chunk holds the question without an answer
    assert any("(continued):" in c.text for c in chunks)
    assert max(c.token_count for c in chunks) <= narrative_settings.chunk_tokens + 20


def test_prepared_remarks_chunks_carry_the_speaker(narrative_settings) -> None:
    unit = TranscriptUnit(
        "prepared_remarks", "Raj Sharma", "management", None, 3, 3, "Revenue grew. Margins held."
    )
    (chunk,) = chunk_transcript_unit(unit, narrative_settings, {"basis": "standalone"})
    assert (chunk.section, chunk.speaker, chunk.speaker_role) == (
        "prepared_remarks",
        "Raj Sharma",
        "management",
    )


def test_token_count_counts_words_and_punctuation() -> None:
    assert count_tokens("Revenue rose 8.5%, again.") == 9


def test_a_pdf_chunk_never_crosses_a_basis_and_carries_its_pages_basis(narrative_settings) -> None:
    slices = [
        PageSlice(4, "results_notes", "Notes: 1 A standalone note.", basis="standalone"),
        PageSlice(5, "results_notes", "2 More standalone notes.", basis="standalone"),
        PageSlice(6, "results_notes", "Notes: 1 A group note.", basis="consolidated"),
        PageSlice(7, "results_notes", "2 A note on no page heading.", basis=None),
        PageSlice(8, "press_release", "News release text.", basis="unknown"),
    ]
    chunks = chunk_results_slices(slices, set(), narrative_settings, {"basis": "consolidated"})
    assert [(c.page, c.meta["end_page"], c.meta["basis"]) for c in chunks] == [
        (4, 5, "standalone"),
        (6, 6, "consolidated"),
        (7, 7, "consolidated"),  # no heading named a basis: the filing's
        (8, 8, "unknown"),
    ]
    assert [c.section for c in chunks][-1] == "press_release"
