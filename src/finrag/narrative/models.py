"""Plain data types shared by the M08 modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REASON_NOT_INDEXED = "not_indexed"
REASON_UNKNOWN = "unknown_section"
REASON_DEFERRED = "deferred"  # annual reports (index_annual_reports: false)

SECTION_NOTES = "results_notes"
SECTION_AUDITOR = "auditor_report"
SECTION_PRESS = "press_release"
BASIS_UNKNOWN = "unknown"
SOURCE_TRANSCRIPT = "transcript"
SOURCE_RESULTS = "results"

ROLE_MANAGEMENT = "management"
ROLE_ANALYST = "analyst"
ROLE_MODERATOR = "moderator"


@dataclass(frozen=True)
class PageSlice:
    """The kept text of one results-PDF page for one section."""

    page: int
    section: str
    text: str
    quality: float | None = None
    partial: bool = False  # leading text of the page (a statement table) was dropped
    basis: str | None = None  # from the page heading (or the pages before it); None = the filing's


@dataclass(frozen=True)
class SkippedSpan:
    """Consecutive pages of a filing that were not indexed, and why."""

    start_page: int
    end_page: int
    reason: str
    detail: str = ""  # first line of the first page, for pattern work


@dataclass
class SkipSummary:
    """Per-filing account of kept and skipped pages (kept + skipped == total)."""

    pages_total: int = 0
    pages_kept: int = 0
    skipped: list[SkippedSpan] = field(default_factory=list)
    partial_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # transcripts: unattributed turns, etc.

    @property
    def pages_skipped(self) -> int:
        """Pages not indexed."""
        return sum(s.end_page - s.start_page + 1 for s in self.skipped)

    def as_json(self, log_skipped: bool = True) -> dict[str, Any]:
        """Summary -> JSON record; with log_skipped false only the totals are kept."""
        record: dict[str, Any] = {
            "pages_total": self.pages_total,
            "pages_kept": self.pages_kept,
            "pages_skipped": self.pages_skipped,
            "partial_pages": self.partial_pages,
            "warnings": self.warnings,
            **self.extra,
        }
        if log_skipped:
            record["skipped"] = [
                {
                    "start_page": s.start_page,
                    "end_page": s.end_page,
                    "reason": s.reason,
                    "detail": s.detail,
                }
                for s in self.skipped
            ]
        return record


@dataclass(frozen=True)
class TranscriptUnit:
    """One indexable unit of a transcript: a management turn, or a whole question and answers."""

    section: str  # prepared_remarks | qa | closing_remarks
    speaker: str | None
    speaker_role: str | None
    firm: str | None
    start_page: int
    end_page: int
    text: str  # prepared/closing: the turn; qa: the question
    answers: tuple[str, ...] = ()  # QA units: "Answer (<name>, <role>): ..." blocks, in order


@dataclass(frozen=True)
class TextBlock:
    """An indexed XBRL text block of a results filing."""

    concept: str
    section: str
    text: str


@dataclass(frozen=True)
class DraftChunk:
    """A chunk before it gets an ID and is written."""

    section: str
    text: str
    token_count: int
    page: int | None
    speaker: str | None = None
    speaker_role: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
