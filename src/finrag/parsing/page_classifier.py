"""Label PDF pages as P&L / balance sheet / cash flow / segment, with basis and unit hints."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from finrag.core.config import Settings, get_settings
from finrag.core.types import Basis
from finrag.parsing.pdf_text import PageText
from finrag.parsing.sections import SectionPatterns


@dataclass(frozen=True)
class PageLabel:
    """What one page looks like; statement_type is None when the score is too low or tied."""

    page: int
    statement_type: str | None
    basis: Basis
    unit_hint: str | None  # a key of settings.units.multipliers
    score: int
    unit_from_page: int | None = None  # set when the unit was taken from an earlier page


def _hits(patterns: list[re.Pattern[str]], text: str) -> int:
    """Number of distinct patterns that match somewhere in the text."""
    return sum(1 for pattern in patterns if pattern.search(text))


def statement_type_of(
    text: str, patterns: SectionPatterns, min_score: int
) -> tuple[str | None, int]:
    """Page text -> (statement type, score); (None, best score) if too weak or tied."""
    scores = {name: _hits(pats, text) for name, pats in patterns.statements.items()}
    best = max(scores.values(), default=0)
    leaders = [name for name, score in scores.items() if score == best]
    if best < min_score or len(leaders) != 1:
        return None, best
    return leaders[0], best


NEWLINE = "\n"


def basis_of(text: str, patterns: SectionPatterns, scan_lines: int) -> Basis:
    """The page's own basis: a basis word in its first lines, else one in a statement title.

    Exactly one kind -> that basis, else UNKNOWN (the caller then uses the filing's label). The
    title is looked for in every line because some text layers put the title block last.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    head = _kinds(NEWLINE.join(lines[:scan_lines]), patterns)
    if head:
        return Basis(head[0]) if len(head) == 1 else Basis.UNKNOWN
    titles = NEWLINE.join(
        line for line in lines if any(p.search(line) for p in patterns.basis_titles)
    )
    found = _kinds(titles, patterns)
    return Basis(found[0]) if len(found) == 1 else Basis.UNKNOWN


def _kinds(text: str, patterns: SectionPatterns) -> list[str]:
    """The basis kinds a text mentions."""
    return [key for key, pats in patterns.basis.items() if _hits(pats, text)]


def unit_hint_of(text: str, patterns: SectionPatterns) -> str | None:
    """Earliest unit phrase on the page ('₹ in crore') -> its unit key, or None."""
    earliest: tuple[int, str] | None = None
    for key, pats in patterns.units.items():
        for pattern in pats:
            match = pattern.search(text)
            if match and (earliest is None or match.start() < earliest[0]):
                earliest = (match.start(), key)
    return earliest[1] if earliest else None


def classify_pages(
    pages: Iterable[PageText], patterns: SectionPatterns, settings: Settings | None = None
) -> list[PageLabel]:
    """Pages -> one PageLabel each (statement type, basis words, unit line, score).

    A statement page with no unit line of its own takes the unit of the latest earlier statement
    page that has one (Netweb states "in millions" once, above the P&L, for all three statements);
    `unit_from_page` says so. Pages that are not statements never inherit.
    """
    settings = settings or get_settings()
    pdf = settings.parsing.pdf
    labels: list[PageLabel] = []
    last_unit: tuple[str, int] | None = None  # (unit, page) of the latest statement page with one
    for page in pages:
        statement, score = statement_type_of(page.text, patterns, pdf.statement_page_min_score)
        unit, from_page = unit_hint_of(page.text, patterns), None
        if statement and unit is None and last_unit:  # one unit line often covers all statements
            unit, from_page = last_unit
        if statement and from_page is None and unit:
            last_unit = (unit, page.page)
        labels.append(
            PageLabel(
                page=page.page,
                statement_type=statement,
                basis=basis_of(page.text, patterns, pdf.basis_scan_lines),
                unit_hint=unit,
                score=score,
                unit_from_page=from_page,
            )
        )
    return labels
