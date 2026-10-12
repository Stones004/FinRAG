import pytest

from finrag.core.config import ConfigError, get_settings
from finrag.ingestion.transcript_check import (
    TranscriptCheckError,
    check_pages,
    check_transcript,
    qa_start_pattern,
)


def test_one_page_letter_with_a_link_is_link_only(kit) -> None:
    check = check_transcript(kit.link_only_letter())
    assert check.is_link_only and check.page_count == 1
    assert "https://www.alpha.example/investors" in check.links
    assert "https://www.alpha.example/investors/transcripts" in check.links
    assert any("website" in line for line in check.link_text)


def test_subject_quarter_is_read_but_only_as_a_claim(kit) -> None:
    assert check_transcript(kit.link_only_letter("Q1 FY26")).subject_quarter == "Q1 FY26"


def test_full_transcript_behind_a_cover_letter_is_not_link_only(kit) -> None:
    check = check_transcript(kit.full_transcript())
    assert not check.is_link_only and check.page_count == 4
    assert check.links == () and check.title_quarter == "Q1 FY27"


def test_short_file_with_qa_text_is_not_link_only() -> None:
    pages = ["cover", "Moderator: The next question is from the line of A B from C."]
    assert not check_pages(pages).is_link_only  # 2 pages (<= max) but Q&A text is present


def test_long_file_without_qa_is_not_link_only() -> None:
    check = check_pages(["page one", "page two", "page three"])
    assert not check.is_link_only  # more pages than the cover-letter limit


def test_page_limit_comes_from_settings() -> None:
    settings = get_settings()
    wider = settings.model_copy(
        update={
            "ingestion": settings.ingestion.model_copy(update={"transcript_link_only_max_pages": 3})
        }
    )
    pages = ["page one", "page two", "page three"]
    assert check_pages(pages, settings=wider).is_link_only
    assert not check_pages(pages, settings=settings).is_link_only


@pytest.mark.parametrize(
    ("line", "label"),
    [
        ("Q1 FY27 Earnings Conference Call", "Q1 FY27"),
        ("Q4 & Full Year FY 2026 Earnings Conference Call", "Q4 FY26"),
        ("Q4 FY '26 Earnings Conference Call", "Q4 FY26"),
    ],
)
def test_title_quarter_formats_seen_in_real_transcripts(line: str, label: str) -> None:
    assert check_pages(["Cover letter", line, "body"]).title_quarter == label


def test_title_in_running_text_is_not_a_title() -> None:
    pages = ["cover", "welcome to Alpha Q1 FY27 Earnings\nConference Call on"]
    assert check_pages(pages).title_quarter is None


def test_unreadable_pdf_raises() -> None:
    with pytest.raises(TranscriptCheckError):
        check_transcript(b"not a pdf")


def test_qa_pattern_is_read_from_sections_yaml(isolated_repo) -> None:
    assert qa_start_pattern().search("The next question is from the line of X")
    (isolated_repo / "config" / "sections.yaml").write_text("transcript_markers: {}\n")
    with pytest.raises(ConfigError, match="qa_start"):
        qa_start_pattern()
