"""Periods that a piece of filing text refers to: 'year ended March 31, 2026', 'Q4 FY26', 'FY25'."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from finrag.core.fiscal import label_to_period

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_LEAD = (
    r"(?:(?:quarter|year|period|half[- ]year|nine[- ]months?|six[- ]months?|three[- ]months?|"
    r"twelve[- ]months?)\s+ended|as\s+(?:at|on))\s+(?:the\s+)?"
)
_MDY = re.compile(rf"\b{_LEAD}{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)
_DMY = re.compile(rf"\b{_LEAD}(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}\.?,?\s+(\d{{4}})\b", re.I)
_LABEL = re.compile(r"\b(?:([QH][1-4]|9M)\s*)?FY\s*'?(\d{2}|\d{4})\b", re.I)


@dataclass(frozen=True)
class PeriodRef:
    """A period named in text: the words, and the dates it covers (a single date for 'as at')."""

    text: str
    start: date
    end: date


def period_refs(text: str) -> list[PeriodRef]:
    """Text -> every dated period it names (by 'ended' / 'as at' dates and by FY labels)."""
    refs: list[PeriodRef] = []
    for m in _MDY.finditer(text):
        refs += _dated(m.group(0), m.group(1), m.group(2), m.group(3))
    for m in _DMY.finditer(text):
        refs += _dated(m.group(0), m.group(2), m.group(1), m.group(3))
    for m in _LABEL.finditer(text):
        yy = m.group(2)[-2:]
        label = f"{(m.group(1) or '').upper()} FY{yy}".strip()
        try:
            period = label_to_period(label)
        except ValueError:
            continue
        refs.append(PeriodRef(m.group(0), period.start, period.end))
    return refs


def _dated(shown: str, month: str, day: str, year: str) -> list[PeriodRef]:
    """Month name, day and year as found -> a one-date reference ([] if not a real date)."""
    try:
        when = date(int(year), _MONTHS[month.lower()], int(day))
    except ValueError:
        return []
    return [PeriodRef(shown, when, when)]
