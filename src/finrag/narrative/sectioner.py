"""Results PDFs: find the notes and auditor's / limited-review report text; account for the rest.

A page's lines are scanned for heading lines. A heading starts a span (`results_notes` or
`auditor_report`); the span runs over the following pages until a statement page, a page that
opens an annexure / news release / covering letter, or (auditor report) the page that carries
the signature (UDIN). Text before a heading on a page that is a financial statement is dropped
(a notes heading often sits at the bottom of the statement page); such pages are listed as
partial. Annual reports are not sectioned in v1 (deferred, see docs/modules/M08_narrative.md).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from finrag.narrative.models import (
    BASIS_UNKNOWN,
    REASON_NOT_INDEXED,
    REASON_UNKNOWN,
    SECTION_AUDITOR,
    SECTION_NOTES,
    SECTION_PRESS,
    PageSlice,
    SkippedSpan,
    SkipSummary,
)
from finrag.narrative.patterns import ResultsPatterns

DETAIL_CHARS = 80


@dataclass(frozen=True)
class PageInput:
    """One results-PDF page as the sectioner sees it."""

    page: int
    text: str
    statement_type: str | None = None  # M03 page classifier label
    quality: float | None = None
    basis: str | None = None  # the page heading's basis (M03 classifier), None when it names none


def _lines(text: str) -> list[str]:
    """Page text -> non-blank stripped lines."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _first_match(lines: Sequence[str], patterns: Sequence[re.Pattern[str]]) -> int | None:
    """Index of the first line matching any pattern, or None."""
    for index, line in enumerate(lines):
        if any(p.search(line) for p in patterns):
            return index
    return None


def _headings(lines: Sequence[str], patterns: ResultsPatterns) -> list[tuple[int, str]]:
    """Heading lines of a page -> [(line index, section)] in page order."""
    found: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if any(p.search(line) for p in patterns.auditor_heading):
            found.append((index, SECTION_AUDITOR))
        elif any(p.search(line) for p in patterns.notes_heading):
            found.append((index, SECTION_NOTES))
    return found


def _is_not_narrative(lines: Sequence[str], patterns: ResultsPatterns) -> bool:
    """True when the page opens an annexure / news release / covering letter."""
    head = lines[: patterns.not_narrative_head_lines]
    return _first_match(head, patterns.not_narrative) is not None


def _continues_notes(
    lines: Sequence[str], page: PageInput, allowed: bool, patterns: ResultsPatterns
) -> bool:
    """True for an unclassified page of numbered items that continues earlier notes."""
    if not allowed or page.statement_type is not None:
        return False
    numbered = sum(1 for line in lines if patterns.continuation.search(line))
    return numbered >= patterns.continuation_min_lines


class _Skips:
    """Collects skipped pages into runs of consecutive pages with the same reason."""

    def __init__(self) -> None:
        self.spans: list[SkippedSpan] = []

    def add(self, page: int, reason: str, detail: str) -> None:
        """Skip one page; extends the previous run when adjacent with the same reason."""
        last = self.spans[-1] if self.spans else None
        if last and last.end_page == page - 1 and last.reason == reason:
            self.spans[-1] = SkippedSpan(last.start_page, page, reason, last.detail)
        else:
            self.spans.append(SkippedSpan(page, page, reason, detail[:DETAIL_CHARS]))


def section_results_pdf(
    pages: Sequence[PageInput], patterns: ResultsPatterns
) -> tuple[list[PageSlice], SkipSummary]:
    """Results-PDF pages -> (kept slices in page order, summary of kept and skipped pages)."""
    kept: list[PageSlice] = []
    skips = _Skips()
    summary = SkipSummary(pages_total=len(pages))
    state: str | None = None  # section of the running span
    notes_seen = False  # a notes heading has been seen (continuation pages are possible)
    broken = False  # an annexure / news release, or the end of an auditor report, since a heading
    press = False  # a press release has begun: it runs to the end of the document
    basis: str | None = None  # the basis named by the latest page heading seen (document order)
    for page in pages:
        lines = _lines(page.text)
        first = lines[0] if lines else ""
        head = lines[: patterns.not_narrative_head_lines]
        if page.basis:  # a page that names exactly one basis sets it for the pages that follow
            basis = page.basis
        press = press or _first_match(head, patterns.press_start) is not None
        if press:
            state, broken = None, True
            if page.statement_type or not lines:  # tables of the release repeat the results
                skips.add(page.page, REASON_NOT_INDEXED, f"press release table: {first}")
                continue
            summary.pages_kept += 1
            kept.append(
                PageSlice(
                    page.page, SECTION_PRESS, "\n".join(lines), page.quality, False, BASIS_UNKNOWN
                )
            )
            continue
        if _is_not_narrative(lines, patterns):
            state, broken, basis = None, True, None
            skips.add(page.page, REASON_NOT_INDEXED, first)
            continue
        marks = _headings(lines, patterns)
        # (section, lines) pieces of this page that are kept
        pieces: list[tuple[str, list[str]]] = []
        lead = lines[: marks[0][0]] if marks else lines
        lead_kept = False
        if state and lead:
            if page.statement_type is None:
                pieces.append((state, lead))
                lead_kept = True
            else:  # a statement page without a heading closes the span
                state = None
        for number, (index, section) in enumerate(marks):
            end = marks[number + 1][0] if number + 1 < len(marks) else len(lines)
            pieces.append((section, lines[index:end]))
            state = section
        if marks:
            broken = False
            notes_seen = notes_seen or any(section == SECTION_NOTES for _, section in marks)
        elif not pieces and _continues_notes(lines, page, notes_seen and not broken, patterns):
            pieces.append((SECTION_NOTES, lines))
            state, lead_kept = SECTION_NOTES, True
        partial = bool(lead) and not lead_kept  # text before the first heading was dropped
        if state == SECTION_AUDITOR and pieces:
            tail = pieces[-1][1] if pieces[-1][0] == SECTION_AUDITOR else []
            if _first_match(tail, patterns.auditor_end) is not None:
                state, broken = None, True
        if not pieces:
            reason = REASON_NOT_INDEXED if page.statement_type else REASON_UNKNOWN
            skips.add(page.page, reason, first)
            continue
        summary.pages_kept += 1
        if partial:
            summary.partial_pages.append(page.page)
        for section, piece in pieces:
            kept.append(
                PageSlice(
                    page.page, section, "\n".join(piece), page.quality, partial=partial, basis=basis
                )
            )
    summary.skipped = skips.spans
    return kept, summary


@dataclass(frozen=True)
class SectionSpan:
    """A run of kept pages in one section."""

    section: str
    start_page: int
    end_page: int


def spans_of(slices: Sequence[PageSlice]) -> list[SectionSpan]:
    """Kept slices -> spans of consecutive pages in the same section."""
    spans: list[SectionSpan] = []
    for item in slices:
        last = spans[-1] if spans else None
        if last and last.section == item.section and item.page <= last.end_page + 1:
            spans[-1] = SectionSpan(last.section, last.start_page, max(last.end_page, item.page))
        else:
            spans.append(SectionSpan(item.section, item.page, item.page))
    return spans
