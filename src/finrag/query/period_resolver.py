"""Question -> periods and comparison: regex patterns, relative periods, defaults.

Two phases: `parse_periods` reads the text (pure, consumes what it understood); `resolve_periods`
turns the mentions into periods against today's date and the data the company actually has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from finrag.core.config import QuerySettings
from finrag.core.fiscal import (
    components_of,
    fiscal_year_of,
    fy_label,
    label_to_period,
    period_to_label,
    shift_period,
)
from finrag.core.types import Period, PeriodType
from finrag.query.availability import Availability
from finrag.query.intents import Intents
from finrag.query.text import Working

_CENTURY = 2000
_FOUR_DIGIT_YEAR = 4
_MAX_WALK_BACK = 12
_MONTH = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


@dataclass(frozen=True)
class PeriodMention:
    """A period expression found in the question."""

    kind: str
    label: str  # fiscal label for explicit periods, count for last_n_years, "" otherwise
    start: int


@dataclass
class PeriodParse:
    """Phase 1: what the text says, before any date is looked at."""

    mentions: list[PeriodMention] = field(default_factory=list)
    comparison: str | None = None  # yoy | sequential | basis from comparison words
    latest: bool = False  # "latest", "most recent": the default period, asked for on purpose
    trend_word: bool = False


@dataclass
class PeriodResolution:
    """Phase 2: periods, comparison, confidence and what was defaulted or substituted."""

    periods: list[Period] = field(default_factory=list)
    comparison: str | None = None
    confidence: float = 1.0
    defaulted: bool = False
    notes: list[str] = field(default_factory=list)  # shown on the interpretation line
    future: list[Period] = field(
        default_factory=list
    )  # named periods that end after the latest filed
    latest_filed: Period | None = None  # the company's latest period with any data
    transcript_default: bool = False  # the default period is the latest one with a transcript


def describe_period(period: Period) -> str:
    """Period -> "Q1 FY27 (Apr-Jun 2026)" / "FY26 (Apr 2025-Mar 2026)"."""
    if period.type is PeriodType.INSTANT:
        return period.end.isoformat()
    label = period_to_label(period)
    a, b = period.start, period.end
    if a.year == b.year:
        span = f"{_MONTH[a.month - 1]}–{_MONTH[b.month - 1]} {b.year}"
    else:
        span = f"{_MONTH[a.month - 1]} {a.year}–{_MONTH[b.month - 1]} {b.year}"
    return f"{label} ({span})"


def _year(text: str) -> int:
    """'26' | '2026' -> 2026."""
    value = int(text)
    return value if len(text) == _FOUR_DIGIT_YEAR else _CENTURY + value


def _label(kind: str, groups: tuple[str, ...]) -> str:
    """A period regex's groups -> fiscal label such as 'Q1 FY26'."""
    yy = f"{_year(groups[-1]) % 100:02d}"
    prefix = {
        "quarter": f"Q{groups[0]} ",
        "half": f"H{groups[0]} ",
        "nine_months": "9M ",
        "fiscal_year": "",
    }[kind]
    return f"{prefix}FY{yy}"


def parse_periods(work: Working, intents: Intents) -> PeriodParse:
    """Working question -> period mentions and comparison words (consumes them)."""
    parse = PeriodParse()
    for kind, words in intents.comparison_words.items():
        for phrase in sorted(words, key=len, reverse=True):
            for span in work.find(phrase):
                parse.comparison = parse.comparison or kind
                work.consume(span)
    for pattern in intents.period_patterns:
        for match in work.search(re.compile(pattern.regex)):
            if pattern.kind in ("quarter", "half", "nine_months", "fiscal_year"):
                label = _label(pattern.kind, match.groups())
            elif pattern.kind == "last_n_years":
                label = match.group(1)
            else:
                label = ""
            parse.mentions.append(PeriodMention(pattern.kind, label, match.start()))
            work.consume(match)
    for word in intents.latest_words:
        for span in work.find(word):
            parse.latest = True
            work.consume(span)
    parse.trend_word = any(w.strip() and w.strip() in work.text for w in intents.trend_words)
    parse.mentions.sort(key=lambda m: m.start)
    return parse


def calendar_last_quarter(today: date) -> Period:
    """Today -> the fiscal quarter before the one containing today."""
    year = fiscal_year_of(today)
    for number in (1, 2, 3, 4):
        quarter = label_to_period(f"Q{number} FY{year % 100:02d}")
        if quarter.start <= today <= quarter.end:
            return shift_period(quarter, periods=-1)
    raise ValueError(f"no fiscal quarter contains {today}")  # pragma: no cover


def last_completed_fy(today: date) -> Period:
    """Today -> the last fiscal year that has ended."""
    return label_to_period(fy_label(fiscal_year_of(today) - 1))


def _latest_of_type(
    avail: Availability, isin: str, kind: PeriodType, ids: list[str], concepts: list[str]
) -> Period | None:
    """The latest period of one type with data (the default period filtered to a type)."""
    latest = avail.latest(isin, ids, concepts)
    if latest is None or latest.type is kind:
        return latest
    ref = latest.end
    for _ in range(_MAX_WALK_BACK):
        candidate = _period_ending_by(kind, ref)
        if avail.has_period(isin, candidate):
            return candidate
        ref = candidate.start - timedelta(days=1)
    return None


def _period_ending_by(kind: PeriodType, ref: date) -> Period:
    """The most recent fiscal quarter or year that ends on or before `ref`."""
    year = fiscal_year_of(ref)
    names = ["Q4", "Q3", "Q2", "Q1"] if kind is PeriodType.Q else [""]
    found = [
        label_to_period(f"{name} FY{y % 100:02d}".strip())
        for y in (year, year - 1)
        for name in names
    ]
    return max((p for p in found if p.end <= ref), key=lambda p: p.end)


def resolve_periods(
    parse: PeriodParse,
    isin: str,
    metric_ids: list[str],
    concept_ids: list[str],
    avail: Availability,
    settings: QuerySettings,
    today: date,
    forward: bool = False,
    transcript_default: bool = False,
) -> PeriodResolution:
    """Mentions -> periods against today and the stored data (see the module docstring).

    No period named: the latest period with data, except for a management or narrative question
    (`transcript_default`), which takes the latest period that has a transcript - what management
    said exists only for the calls held - and falls back to the latest with data when the company
    has no transcript at all.

    A named period that ends after the company's latest filed period is not evidence: it goes to
    `future` (the caller refuses it, or, for a forward-looking question, treats it as the horizon).
    A period before the data starts stays in `periods`: the answer says there is no data.
    """
    out = PeriodResolution(comparison=parse.comparison)
    out.latest_filed = avail.latest(isin, [], [])
    cap = settings.intent
    for mention in parse.mentions:
        if mention.kind in ("quarter", "half", "nine_months", "fiscal_year"):
            named = label_to_period(mention.label)
            if out.latest_filed is not None and named.end > out.latest_filed.end:
                if named not in out.future:
                    out.future.append(named)
            else:
                _add(out, named)
        elif mention.kind == "last_n_years":
            count = min(max(int(mention.label), 1), cap.max_last_n_years)
            last = last_completed_fy(today)
            for back in range(count - 1, -1, -1):
                _add(out, shift_period(last, years=-back))
            out.comparison = out.comparison or "trend"
        elif mention.kind == "last_quarter":
            _relative(
                out,
                calendar_last_quarter(today),
                PeriodType.Q,
                "quarter",
                isin,
                metric_ids,
                concept_ids,
                avail,
                settings,
                today,
            )
        elif mention.kind == "last_fiscal_year":
            _relative(
                out,
                last_completed_fy(today),
                PeriodType.FY,
                "fiscal year",
                isin,
                metric_ids,
                concept_ids,
                avail,
                settings,
                today,
            )
    if not out.periods and (forward or not out.future):
        latest = avail.latest(isin, metric_ids, concept_ids)
        called = avail.latest_transcript(isin) if transcript_default else None
        out.defaulted = True
        out.confidence = cap.default_period_confidence
        if called is not None:
            out.periods.append(called)
            out.transcript_default = True
        elif latest is None:
            out.notes.append("no filed data was found for this company")
        else:
            out.periods.append(latest)
    elif len(out.periods) > 1 and out.comparison is None:
        out.comparison = "periods"
    if out.comparison is None and parse.trend_word:
        out.comparison = "trend"
    return out


def _add(out: PeriodResolution, period: Period) -> None:
    """Append a period once."""
    if period not in out.periods:
        out.periods.append(period)


def _relative(
    out: PeriodResolution,
    wanted: Period,
    kind: PeriodType,
    noun: str,
    isin: str,
    metric_ids: list[str],
    concept_ids: list[str],
    avail: Availability,
    settings: QuerySettings,
    today: date,
) -> None:
    """A relative period: the period itself if filed, else the latest filed one and a note."""
    if avail.has_period(isin, wanted):
        _add(out, wanted)
        return
    shown = _latest_of_type(avail, isin, kind, metric_ids, concept_ids)
    if shown is None:
        _add(out, wanted)
        out.notes.append(f"{describe_period(wanted)} is not filed and no earlier {noun} has data")
        out.confidence = min(out.confidence, settings.intent.unfiled_period_confidence)
        return
    _add(out, shown)
    out.notes.append(
        f"{describe_period(wanted)} is not filed; "
        f"showing {describe_period(shown)}, the latest filed"
    )
    out.confidence = min(out.confidence, settings.intent.unfiled_period_confidence)


def latest_quarters(latest: Period, count: int) -> list[Period]:
    """Latest filed period -> the `count` quarters ending with its last quarter, oldest first."""
    last = (components_of(latest) or [latest])[-1]
    return [shift_period(last, periods=-back) for back in range(count - 1, -1, -1)]
