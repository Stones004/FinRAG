"""Earnings-call transcripts: cover letter off, turns, sections, speaker roles, Q&A units.

Input is the page records of `processed/<filing_id>/pages.jsonl` (`page`, `text`, and for native
pages `lines: [{text, bold, font, size}]`). Speaker labels are read from the lines (bold at line
start) with the text pattern; a turn that cannot be attributed is reported, never guessed.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from finrag.narrative.models import (
    ROLE_ANALYST,
    ROLE_MANAGEMENT,
    ROLE_MODERATOR,
    SkipSummary,
    TranscriptUnit,
)
from finrag.narrative.patterns import TranscriptPatterns

SECTION_PREPARED = "prepared_remarks"
SECTION_QA = "qa"
SECTION_CLOSING = "closing_remarks"
HONORIFICS = frozenset({"mr", "ms", "mrs", "dr", "shri", "smt"})
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Row:
    """One line of transcript text with its page and bold flag."""

    page: int
    text: str
    bold: bool
    has_font: bool  # the page carries per-line font info (a native page)


@dataclass(frozen=True)
class Participant:
    """A person from the cover participant block."""

    name: str  # title case
    title: str  # role words, title case ("Chief Financial Officer")
    company: str
    group: str  # management | moderator


@dataclass
class Turn:
    """One speaker turn: label, text, pages, and the role once resolved."""

    speaker: str | None
    text: str
    start_page: int
    end_page: int
    role: str | None = None
    firm: str | None = None
    title: str = ""
    label_lost: bool = False  # created for text whose label the PDF lost (after an analyst intro)
    index: int = 0


@dataclass(frozen=True)
class Intro:
    """An analyst introduction found in a turn."""

    turn_index: int
    name: str
    firm: str | None
    start: int  # character span of the intro sentence within the turn text
    end: int


@dataclass
class TranscriptResult:
    """Units plus the summary lists the sectioner reports."""

    units: list[TranscriptUnit] = field(default_factory=list)
    participants: list[Participant] = field(default_factory=list)
    summary: SkipSummary = field(default_factory=SkipSummary)


def norm_name(name: str) -> str:
    """Name -> lower-case tokens without honorifics and dots."""
    tokens = [t for t in re.sub(r"[.,]", " ", name.lower()).split() if t not in HONORIFICS]
    return " ".join(tokens)


def _rows(pages: Sequence[dict[str, Any]]) -> list[Row]:
    """Page records -> rows (from font lines where present, else from the text lines)."""
    rows: list[Row] = []
    for page in pages:
        lines = page.get("lines")
        if lines:
            for line in lines:
                text = _SPACE.sub(" ", str(line["text"])).strip()
                if text:
                    rows.append(Row(page["page"], text, bool(line.get("bold")), True))
        else:
            for text in str(page.get("text", "")).splitlines():
                text = _SPACE.sub(" ", text).strip()
                if text:
                    rows.append(Row(page["page"], text, False, False))
    return rows


def _furniture(rows: Sequence[Row], pages: int, patterns: TranscriptPatterns) -> set[str]:
    """Normalised lines repeated in the head of at least furniture_min_share of the pages."""
    per_page: dict[int, list[str]] = {}
    for row in rows:
        per_page.setdefault(row.page, []).append(row.text)
    counts: Counter[str] = Counter()
    for texts in per_page.values():
        for text in set(t.lower() for t in texts[: patterns.furniture_head_lines]):
            counts[text] += 1
    need = max(3, patterns.furniture_min_share * max(pages, 1))
    return {text for text, n in counts.items() if n >= need}


def _strip_furniture(rows: Sequence[Row], patterns: TranscriptPatterns) -> list[Row]:
    """Drop footers, repeated page headers and furniture lines; trim header text off labels."""
    page_count = len({r.page for r in rows})
    repeated = _furniture(rows, page_count, patterns)
    static = {s.lower() for s in patterns.furniture_lines}
    out: list[Row] = []
    head_seen: dict[int, int] = {}
    for row in rows:
        position = head_seen.get(row.page, 0)
        head_seen[row.page] = position + 1
        lowered = row.text.lower()
        in_head = position < patterns.furniture_head_lines
        if patterns.footer.match(row.text) or lowered in static:
            continue
        if in_head and lowered in repeated:
            continue
        text = row.text
        if in_head:  # "Moderator:    HDFC Bank Limited" -> "Moderator:"
            for phrase in sorted(repeated, key=len, reverse=True):
                if lowered.endswith(phrase) and len(lowered) > len(phrase):
                    text = text[: len(text) - len(phrase)].rstrip()
                    break
        out.append(Row(row.page, text, row.bold, row.has_font))
    return out


def _label(row: Row, patterns: TranscriptPatterns) -> tuple[str, str, bool] | None:
    """A row -> (speaker, rest of the row, trusted) if it looks like the start of a turn.

    On a native page a real label is bold; a bold line ending in ":" counts as a label even when
    the text pattern misses it (font fallback). A label-shaped line that is not bold is returned
    untrusted: the caller starts an unattributed turn instead of guessing a speaker.
    """
    match = patterns.speaker_line.match(row.text)
    if match:
        speaker = match.group("speaker")
        if speaker not in patterns.not_speaker and not (speaker.isupper() and len(speaker) > 3):
            trusted = row.bold or not row.has_font
            return speaker, (match.group("text") or "").strip(), trusted
    if row.bold and row.has_font and row.text.endswith(":") and len(row.text.split()) <= 6:
        speaker = row.text[:-1].strip()
        if speaker and speaker not in patterns.not_speaker and not speaker.isupper():
            return speaker, "", True
    return None


def _participants(rows: Sequence[Row], patterns: TranscriptPatterns) -> list[Participant]:
    """The cover block rows -> participants with group and role title."""
    text = " ".join(r.text for r in rows)
    marks = list(patterns.participant_block.finditer(text))
    out: list[Participant] = []
    for number, mark in enumerate(marks):
        end = marks[number + 1].start() if number + 1 < len(marks) else len(text)
        group = ROLE_MODERATOR if mark.group("group").upper() == "MODERATOR" else ROLE_MANAGEMENT
        body = text[mark.end() : end]
        starts = [m.start() for m in patterns.participant_entry.finditer(body)]
        for i, start in enumerate(starts):
            stop = starts[i + 1] if i + 1 < len(starts) else len(body)
            entry = patterns.participant_entry.sub("", body[start:stop], count=1).strip()
            parts = [p.strip(" ,") for p in patterns.participant_sep.split(entry) if p.strip(" ,-")]
            if not parts:
                continue
            out.append(
                Participant(
                    name=parts[0].title(),
                    title=parts[1].title() if len(parts) > 1 else "",
                    company=" - ".join(parts[2:]).title() if len(parts) > 2 else "",
                    group=group,
                )
            )
    return out


def _find_participant(speaker: str, participants: Sequence[Participant]) -> Participant | None:
    """Speaker label -> participant by full name, or by label tokens contained in the name."""
    wanted = norm_name(speaker)
    tokens = set(wanted.split())
    for person in participants:
        if norm_name(person.name) == wanted:
            return person
    if len(tokens) >= 2:
        for person in participants:
            if tokens <= set(norm_name(person.name).split()):
                return person
    return None


def _same_person(label: str, intro_name: str) -> bool:
    """Speaker label vs the name in an analyst intro (full name, or first name only)."""
    a, b = norm_name(label), norm_name(intro_name)
    if a == b:
        return True
    a_tokens, b_tokens = a.split(), b.split()
    if not a_tokens or not b_tokens:
        return False
    if len(b_tokens) == 1 and a_tokens[0] == b_tokens[0]:
        return True
    return len(a_tokens) == 1 and b_tokens[0] == a_tokens[0]


def _clean_firm(firm: str) -> str | None:
    """Firm text from an intro -> firm, or None when the intro gave none."""
    cleaned = re.sub(r"(?i)\bplease go ahead\b.*$", "", firm).strip(" .,")
    if not cleaned or cleaned.startswith("-") or re.search(r"(?i)\bwho is\b", cleaned):
        return None
    return cleaned


def _intros(turn: Turn, patterns: TranscriptPatterns) -> list[Intro]:
    """All analyst introductions in one turn."""
    found: list[Intro] = []
    for match in patterns.analyst_intro.finditer(turn.text):
        name = _SPACE.sub(" ", match.group("name")).strip(" ,.")
        if not name:
            continue
        sentence_start = max(turn.text.rfind(". ", 0, match.start()) + 2, 0)
        sentence_start = sentence_start if sentence_start <= match.start() else 0
        # the intro sentence ends at the match end (the firm stops at "." or "Please go ahead")
        found.append(
            Intro(turn.index, name, _clean_firm(match.group("firm")), sentence_start, match.end())
        )
    return found


def _segment(
    rows: Sequence[Row],
    patterns: TranscriptPatterns,
    participants: Sequence[Participant],
    summary: SkipSummary,
) -> list[Turn]:
    """Rows after the participant block -> turns (text before the first label is dropped).

    A label-shaped line that is not bold is accepted only when it names a cover-block participant
    (the PDF did not bold it; listed in the summary); otherwise it starts an unattributed turn.
    """
    turns: list[Turn] = []
    for row in rows:
        label = _label(row, patterns)
        if label:
            speaker, rest, trusted = label
            if not trusted and _find_participant(speaker, participants):
                trusted = True
                summary.extra.setdefault("non_bold_labels", []).append(
                    {"page": row.page, "speaker": speaker}
                )
            if trusted:
                turns.append(Turn(speaker, rest, row.page, row.page, index=len(turns)))
            else:  # label-shaped but not bold: report it, do not guess who is speaking
                turns.append(Turn(None, row.text, row.page, row.page, index=len(turns)))
        elif turns:
            turn = turns[-1]
            turn.text = f"{turn.text} {row.text}".strip()
            turn.end_page = row.page
    return turns


def _operator_words(text: str, phrases: Sequence[str]) -> int:
    """Words of `text` outside operator sentences ("please go ahead", "unmute your line", ...)."""
    sentences = re.split(r"(?<=[.?!])\s+", text)
    kept = [s for s in sentences if not any(p in s.lower() for p in phrases)]
    return len(" ".join(kept).split())


def split_lost_labels(
    turns: list[Turn], patterns: TranscriptPatterns, summary: SkipSummary
) -> list[Turn]:
    """Operator/IR turns that carry an intro AND a question: the question gets its own turn.

    The label of the analyst's turn was lost by the PDF text. Per the attribution rules the text
    that follows an analyst intro is that analyst's question; it is reported in the summary.
    """
    out: list[Turn] = []
    for turn in turns:
        out.append(turn)
        found = _intros(turn, patterns)
        for intro in found[-1:]:  # only the last intro of a turn can be followed by a lost label
            tail = turn.text[intro.end :].strip(" .")
            tail = re.sub(r"(?i)^\s*please go ahead\.?", "", tail).strip()
            if _operator_words(tail, patterns.operator_phrases) >= patterns.lost_label_min_words:
                turn.text = turn.text[: intro.end]
                out.append(
                    Turn(
                        intro.name,
                        tail,
                        turn.end_page,
                        turn.end_page,
                        label_lost=True,
                        firm=intro.firm,
                    )
                )
                summary.extra.setdefault("label_lost_after_intro", []).append(
                    {"page": turn.end_page, "analyst": intro.name}
                )
    for index, turn in enumerate(out):
        turn.index = index
    return out


def _qa_bounds(
    turns: Sequence[Turn], patterns: TranscriptPatterns
) -> tuple[int | None, int | None]:
    """(first turn of Q&A, turn that hands the call back for closing remarks)."""
    start = next((t.index for t in turns if patterns.qa_start.search(t.text)), None)
    if start is None:
        return None, None
    end = next(
        (t.index for t in turns if t.index >= start and patterns.qa_end.search(t.text)), None
    )
    return start, end


def _resolve_roles(
    turns: Sequence[Turn],
    intros: Sequence[Intro],
    participants: Sequence[Participant],
    patterns: TranscriptPatterns,
) -> None:
    """Set role / firm / title on every turn, in order, by the attribution rules (M08 spec).

    Order of rules: operator and generic labels; an analyst intro (overrides the participant list
    from the intro onward); the participant list; before the first analyst intro -> management;
    after it -> management only when answering an analyst or management turn; else no role.
    """
    first_intro: dict[str, Intro] = {}
    for intro in intros:
        first_intro.setdefault(norm_name(intro.name), intro)
    first_analyst_turn = intros[0].turn_index if intros else None
    previous: str | None = None  # role of the previous non-operator turn
    for turn in turns:
        speaker = turn.speaker or ""
        person = _find_participant(speaker, participants) if speaker else None
        if person:
            turn.title = person.title
        intro = next(
            (
                i
                for i in first_intro.values()
                if i.turn_index <= turn.index and speaker and _same_person(speaker, i.name)
            ),
            None,
        )
        operator = speaker in patterns.moderator_labels
        if operator:
            turn.role = ROLE_MODERATOR
        elif speaker in patterns.management_labels:
            turn.role = ROLE_MANAGEMENT
        elif intro is not None:
            turn.role, turn.firm = ROLE_ANALYST, turn.firm or intro.firm
        elif person is not None:
            turn.role = person.group
        elif not speaker:
            turn.role = None
        elif first_analyst_turn is None or turn.index < first_analyst_turn:
            turn.role = ROLE_MANAGEMENT
        elif previous in (ROLE_ANALYST, ROLE_MANAGEMENT):
            turn.role = ROLE_MANAGEMENT
        else:
            turn.role = None
        if not operator:
            previous = turn.role


def _answer_prefix(turn: Turn) -> str:
    """'Answer (<name>, <role title>):' for a management reply."""
    if turn.speaker is None:
        return "Answer (unattributed):"
    detail = f"{turn.speaker}, {turn.title}" if turn.title else turn.speaker
    return f"Answer ({detail}):"


def build_units(
    turns: Sequence[Turn],
    intros: Sequence[Intro],
    qa_start: int | None,
    qa_end: int | None,
    participants: Sequence[Participant],
    summary: SkipSummary,
) -> list[TranscriptUnit]:
    """Resolved turns -> units (prepared remarks, Q&A units, closing remarks)."""
    units: list[TranscriptUnit] = []
    boundary = qa_start if qa_start is not None else len(turns)
    for turn in turns[:boundary]:
        if turn.role == ROLE_MODERATOR and turn.speaker == "Moderator":
            continue
        if turn.text.strip():
            units.append(_simple_unit(SECTION_PREPARED, turn))
    if qa_start is not None:
        stop = qa_end if qa_end is not None else len(turns)
        units.extend(_qa_units(turns[qa_start : stop + 1], intros, summary))
        if qa_end is not None:
            for turn in turns[qa_end + 1 :]:
                if turn.role == ROLE_MODERATOR and turn.speaker == "Moderator":
                    continue
                if turn.text.strip():
                    units.append(_simple_unit(SECTION_CLOSING, turn))
        else:
            summary.warnings.append("no closing hand-over found: no closing_remarks section")
    else:
        summary.warnings.append("no Q&A start found: everything is prepared_remarks")
    return units


def _simple_unit(section: str, turn: Turn) -> TranscriptUnit:
    """A prepared or closing turn -> one unit."""
    return TranscriptUnit(
        section, turn.speaker, turn.role, turn.firm, turn.start_page, turn.end_page, turn.text
    )


def _qa_units(
    turns: Sequence[Turn], intros: Sequence[Intro], summary: SkipSummary
) -> list[TranscriptUnit]:
    """Turns of the Q&A zone -> one unit per analyst introduction.

    Operator turns and the intro sentences are left out of the text; the firm is kept. The
    analyst's turns before the first answer form the question; later analyst turns are
    follow-ups inside the unit; every other speaker's turn is an answer.
    """
    by_turn: dict[int, list[Intro]] = {}
    for intro in intros:
        by_turn.setdefault(intro.turn_index, []).append(intro)
    units: list[TranscriptUnit] = []
    current: dict[str, Any] | None = None

    def close() -> None:
        nonlocal current
        if current and current["question"]:
            units.append(
                TranscriptUnit(
                    SECTION_QA,
                    current["name"],
                    ROLE_ANALYST,
                    current["firm"],
                    current["start"],
                    current["end"],
                    " ".join(current["question"]),
                    tuple(current["body"]),
                )
            )
        elif current:
            summary.warnings.append(f"analyst {current['name']} was introduced but asked nothing")
        current = None

    for turn in turns:
        here = by_turn.get(turn.index, [])
        if here:
            head = turn.text[: min(i.start for i in here)].strip()
            if head and turn.role == ROLE_MANAGEMENT and current is not None:
                current["body"].append(f"{_answer_prefix(turn)} {head}")  # IR adviser's last words
                current["end"] = turn.end_page
            for intro in here:
                close()
                current = {
                    "name": intro.name,
                    "firm": intro.firm,
                    "start": turn.end_page,
                    "end": turn.end_page,
                    "question": [],
                    "body": [],
                }
            continue
        if current is None or turn.role == ROLE_MODERATOR and turn.speaker in ("Moderator",):
            continue
        if not turn.text.strip():
            continue
        current["end"] = turn.end_page
        if turn.role == ROLE_ANALYST and _same_person(turn.speaker or "", current["name"]):
            if current["body"]:
                current["body"].append(f"Follow-up ({turn.speaker}): {turn.text}")
            else:
                current["question"].append(turn.text)
                current["start"] = min(current["start"], turn.start_page)
        elif turn.role == ROLE_ANALYST:  # another introduced analyst speaking out of turn
            current["body"].append(f"Follow-up ({turn.speaker}): {turn.text}")
        else:
            current["body"].append(f"{_answer_prefix(turn)} {turn.text}")
    close()
    return units


def section_transcript(
    pages: Sequence[dict[str, Any]], patterns: TranscriptPatterns
) -> TranscriptResult:
    """Transcript page records -> units, participants and a summary of what could not be placed."""
    result = TranscriptResult()
    summary = result.summary
    summary.pages_total = len(pages)
    all_rows = _rows(pages)
    start = next((i for i, r in enumerate(all_rows) if patterns.start.search(r.text)), None)
    if start is None:
        summary.warnings.append("title block (transcript_markers.start) not found: nothing indexed")
        return result
    cover_pages = sorted({r.page for r in all_rows[:start]})
    rows = _strip_furniture(all_rows[start + 1 :], patterns)
    block_end = next(
        (
            i
            for i, r in enumerate(rows)
            if (lab := _label(r, patterns)) and lab[2] and not lab[0].isupper()
        ),
        None,
    )
    if block_end is None:
        summary.warnings.append("no speaker label found after the title block: nothing indexed")
        return result
    result.participants = _participants(rows[:block_end], patterns)
    turns = split_lost_labels(
        _segment(rows[block_end:], patterns, result.participants, summary), patterns, summary
    )
    intros = [i for t in turns for i in _intros(t, patterns)]
    qa_start, qa_end = _qa_bounds(turns, patterns)
    _resolve_roles(turns, intros, result.participants, patterns)
    result.units = build_units(turns, intros, qa_start, qa_end, result.participants, summary)
    kept_pages = {p for u in result.units for p in range(u.start_page, u.end_page + 1)}
    summary.pages_kept = len(kept_pages)
    summary.extra["cover_pages"] = cover_pages
    summary.extra["participants"] = [
        {"name": p.name, "title": p.title, "group": p.group} for p in result.participants
    ]
    summary.extra["unattributed_turns"] = [
        {"page": t.start_page, "index": t.index} for t in turns if t.speaker is None
    ]
    summary.extra["null_role_turns"] = [
        {"page": t.start_page, "speaker": t.speaker} for t in turns if t.role is None
    ]
    summary.extra["analysts"] = [
        {"name": i.name, "firm": i.firm, "turn": i.turn_index} for i in intros
    ]
    summary.extra["turn_count"] = len(turns)
    return result
