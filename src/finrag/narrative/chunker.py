"""Chunking: sections and transcript units -> chunks of about narrative.chunk_tokens tokens.

Token counts are an approximation (words and punctuation marks), identical everywhere, so a
chunk size does not depend on the embedding model. A chunk never crosses a section boundary
(the caller chunks one section at a time) and a transcript question stays with its answer.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from finrag.core.config import NarrativeSettings
from finrag.narrative.models import DraftChunk, PageSlice, TextBlock, TranscriptUnit

_TOKEN = re.compile(r"\w+|[^\w\s]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"“‘'])|\n+")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_DIGITS = re.compile(r"\d+")
FURNITURE_EDGE_LINES = 3
FURNITURE_MIN_PAGES = 3


def count_tokens(text: str) -> int:
    """Text -> approximate token count (words and punctuation marks)."""
    return len(_TOKEN.findall(text))


@dataclass(frozen=True)
class _Sentence:
    text: str
    page: int | None
    tokens: int


def _sentences(text: str, page: int | None, limit: int) -> list[_Sentence]:
    """Text -> sentences; a sentence longer than `limit` tokens is cut on word boundaries."""
    out: list[_Sentence] = []
    for raw in _SENTENCE.split(text):
        sentence = _SPACES.sub(" ", raw).strip()
        if not sentence:
            continue
        tokens = count_tokens(sentence)
        if tokens <= limit:
            out.append(_Sentence(sentence, page, tokens))
            continue
        words, part = sentence.split(), []
        for word in words:
            part.append(word)
            if count_tokens(" ".join(part)) >= limit:
                piece = " ".join(part)
                out.append(_Sentence(piece, page, count_tokens(piece)))
                part = []
        if part:
            piece = " ".join(part)
            out.append(_Sentence(piece, page, count_tokens(piece)))
    return out


def pack(
    sentences: Sequence[_Sentence], size: int, overlap: int, minimum: int
) -> list[list[_Sentence]]:
    """Sentences -> groups of up to `size` tokens; groups overlap by about `overlap` tokens.

    A final group under `minimum` tokens is merged into the one before it, never dropped.
    """
    groups: list[list[_Sentence]] = []
    current: list[_Sentence] = []
    fresh = 0  # sentences in `current` that no earlier group holds
    for sentence in sentences:
        total = sum(s.tokens for s in current)
        if current and fresh and total + sentence.tokens > size:
            groups.append(current)
            carry: list[_Sentence] = []
            held = 0
            for old in reversed(current):
                if held + old.tokens > overlap:
                    break
                carry.insert(0, old)
                held += old.tokens
            if held + sentence.tokens > size:
                carry = []
            current, fresh = carry, 0
        current.append(sentence)
        fresh += 1
    if current and fresh:
        last_tokens = sum(s.tokens for s in current[-fresh:])
        if groups and last_tokens < minimum:
            groups[-1] = groups[-1] + current[-fresh:]
        else:
            groups.append(current)
    return groups


def chunk_text(text: str, page: int | None, settings: NarrativeSettings) -> list[tuple[str, int]]:
    """One section's text -> [(chunk text, token count)]; short text is kept whole."""
    sentences = _sentences(text, page, settings.chunk_tokens)
    groups = pack(
        sentences, settings.chunk_tokens, settings.chunk_overlap_tokens, settings.min_chunk_tokens
    )
    return [(" ".join(s.text for s in g), sum(s.tokens for s in g)) for g in groups]


def _normalise_line(line: str) -> str:
    """Line -> comparison key: lower case, digits as #."""
    return _DIGITS.sub("#", _SPACES.sub(" ", line.strip().lower()))


def repeated_edge_lines(page_texts: Sequence[str]) -> set[str]:
    """Lines that open or close at least 40 % (and 3) of the pages -> normalised keys."""
    counts: Counter[str] = Counter()
    for text in page_texts:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        edge = lines[:FURNITURE_EDGE_LINES] + lines[-FURNITURE_EDGE_LINES:]
        counts.update({_normalise_line(ln) for ln in edge})
    need = max(FURNITURE_MIN_PAGES, 0.4 * len(page_texts))
    return {key for key, n in counts.items() if n >= need and key}


def strip_edges(text: str, furniture: set[str]) -> str:
    """Remove repeated header/footer lines from a page's edge lines."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    keep = []
    for index, line in enumerate(lines):
        at_edge = index < FURNITURE_EDGE_LINES or index >= len(lines) - FURNITURE_EDGE_LINES
        if at_edge and _normalise_line(line) in furniture:
            continue
        keep.append(line)
    return "\n".join(keep)


def _join_lines(text: str) -> str:
    """PDF text lines -> one paragraph (physical line breaks are not sentence breaks)."""
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def chunk_results_slices(
    slices: Sequence[PageSlice],
    furniture: set[str],
    settings: NarrativeSettings,
    meta: dict[str, Any],
) -> list[DraftChunk]:
    """Kept results-PDF slices -> chunks; one pass per run of consecutive pages in one section
    and basis (a chunk never crosses either). The chunk's basis is the run's, from the page
    headings; `meta["basis"]` (the filing's) is used only where no page named one."""
    drafts: list[DraftChunk] = []
    runs: list[list[PageSlice]] = []
    for item in slices:
        last = runs[-1] if runs else None
        if (
            last
            and last[-1].section == item.section
            and last[-1].basis == item.basis
            and item.page - last[-1].page <= 1
        ):
            last.append(item)
        else:
            runs.append([item])
    for run in runs:
        sentences: list[_Sentence] = []
        for item in run:
            body = _join_lines(strip_edges(item.text, furniture))
            sentences.extend(_sentences(body, item.page, settings.chunk_tokens))
        groups = pack(
            sentences,
            settings.chunk_tokens,
            settings.chunk_overlap_tokens,
            settings.min_chunk_tokens,
        )
        for group in groups:
            pages = [s.page for s in group if s.page is not None]
            drafts.append(
                DraftChunk(
                    section=run[0].section,
                    text=" ".join(s.text for s in group),
                    token_count=sum(s.tokens for s in group),
                    page=pages[0] if pages else None,
                    meta={
                        **meta,
                        "basis": run[0].basis or meta.get("basis"),
                        "end_page": pages[-1] if pages else None,
                    },
                )
            )
    return drafts


def chunk_text_block(
    block: TextBlock, settings: NarrativeSettings, meta: dict[str, Any]
) -> list[DraftChunk]:
    """An XBRL text block -> chunks (page NULL, the concept in the metadata)."""
    return [
        DraftChunk(
            section=block.section,
            text=text,
            token_count=tokens,
            page=None,
            meta={**meta, "concept": block.concept},
        )
        for text, tokens in chunk_text(block.text, None, settings)
    ]


def _question_prefix(unit: TranscriptUnit) -> str:
    """'Question (<name>, <firm>):' for the analyst's turn."""
    who = f"{unit.speaker}, {unit.firm}" if unit.firm else str(unit.speaker)
    return f"Question ({who}):"


_TURN_LABEL = re.compile(r"^(Answer|Follow-up) \(([^)]*)\)(?: \(continued\))?:\s*")
_ANALYST_ROLE = "analyst"
_MANAGEMENT_ROLE = "management"


def _answer_turn(block: str, unit: TranscriptUnit) -> tuple[str | None, str | None, str | None]:
    """A Q&A answer block -> (speaker, role, role title) read from its label.

    'Answer (<name>, <title>):' is management; 'Follow-up (<name>):' is the analyst again;
    'Answer (unattributed):' has no speaker, so it is neither (searches limited to a role skip it).
    """
    match = _TURN_LABEL.match(block)
    if match is None:
        return None, None, None
    kind, detail = match.group(1), match.group(2)
    if kind == "Follow-up":
        return detail or unit.speaker, _ANALYST_ROLE, None
    if detail == "unattributed":
        return None, None, None
    name, _, title = detail.partition(", ")
    return name, _MANAGEMENT_ROLE, title or None


def chunk_transcript_unit(
    unit: TranscriptUnit, settings: NarrativeSettings, meta: dict[str, Any]
) -> list[DraftChunk]:
    """A transcript unit -> chunks.

    A management turn of the prepared or closing remarks is chunked as before. A Q&A unit is cut
    by turn: the analyst's question (role analyst), then each reply on its own (role management,
    the speaker named in the label), so that a chunk's speaker role is true for all its text.
    The question is kept in the metadata of its answers (`question`), not in their text.
    """
    base = {**meta, "end_page": unit.end_page}
    if unit.firm:
        base["firm"] = unit.firm

    no_firm = {k: v for k, v in base.items() if k != "firm"}  # the firm is the analyst's

    def draft(
        text: str, speaker, role, extra: dict[str, Any] | None = None, speaker_firm: bool = True
    ) -> DraftChunk:
        return DraftChunk(
            section=unit.section,
            text=text,
            token_count=count_tokens(text),
            page=unit.start_page,
            speaker=speaker,
            speaker_role=role,
            meta={**base, **(extra or {})} if speaker_firm else {**no_firm, **(extra or {})},
        )

    if unit.section != "qa":
        return [
            draft(text, unit.speaker, unit.speaker_role)
            for text, _ in chunk_text(unit.text, unit.start_page, settings)
        ]
    prefix = _question_prefix(unit)
    room = max(settings.chunk_tokens - count_tokens(prefix), settings.chunk_tokens // 2)
    chunks = [
        draft(f"{prefix} {text}", unit.speaker, unit.speaker_role)
        for text, _ in chunk_text(unit.text, unit.start_page, settings)
    ] or [draft(prefix, unit.speaker, unit.speaker_role)]
    question = unit.text[: settings.chunk_tokens]
    for block in unit.answers:
        speaker, role, title = _answer_turn(block, unit)
        extra: dict[str, Any] = {"question": question, "turn": "answer"}
        if title:
            extra["role_title"] = title
        if role == _ANALYST_ROLE:
            extra["turn"] = "follow_up"
        for piece in _split_block(block, room, settings):
            chunks.append(draft(piece, speaker, role, extra, speaker_firm=role == _ANALYST_ROLE))
    return chunks


def _split_block(block: str, room: int, settings: NarrativeSettings) -> list[str]:
    """An answer block longer than `room` tokens -> pieces, each keeping the speaker label."""
    if count_tokens(block) <= room:
        return [block]
    label, _, text = block.partition("): ")
    label = f"{label}):" if text else ""
    body = text or block
    limit = max(room - count_tokens(label) - 1, 1)
    sentences = _sentences(body, None, limit)
    groups = pack(sentences, limit, 0, settings.min_chunk_tokens)
    pieces = []
    for number, group in enumerate(groups):
        head = label if number == 0 else label[:-1] + " (continued):" if label else ""
        pieces.append(" ".join(filter(None, [head, " ".join(s.text for s in group)])))
    return pieces
