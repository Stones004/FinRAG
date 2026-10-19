"""The M08 part of the sections registry, validated and compiled."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, ValidationError

from finrag.core.config import ConfigError, load_registry


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Headings(_Section):
    heading: list[str]
    end: list[str] = []


class _NotNarrativeHead(_Section):
    head_lines: int
    patterns: list[str]


class _PressRelease(_Section):
    start: list[str]


class _Continuation(_Section):
    pattern: str
    min_lines: int


class _NarrativeSections(_Section):
    results_notes: _Headings
    auditor_report: _Headings
    notes_continuation: _Continuation
    not_narrative_head: _NotNarrativeHead
    press_release: _PressRelease


class _TranscriptMarkers(_Section):
    start: str
    footer_line: str
    speaker_line: str
    not_speaker: list[str]
    generic_speakers: dict[str, list[str]]
    participant_block: str
    participant_entry: str
    participant_sep: str
    analyst_intro: str
    qa_start: str
    qa_end: str
    operator_phrases: list[str]
    furniture_lines: list[str]
    furniture_head_lines: int
    furniture_min_share: float
    lost_label_min_words: int


@dataclass(frozen=True)
class ResultsPatterns:
    """Compiled heading patterns for the notes and auditor pages of a results PDF."""

    notes_heading: list[re.Pattern[str]]
    auditor_heading: list[re.Pattern[str]]
    auditor_end: list[re.Pattern[str]]
    not_narrative: list[re.Pattern[str]]
    not_narrative_head_lines: int
    press_start: list[re.Pattern[str]]
    continuation: re.Pattern[str]
    continuation_min_lines: int


@dataclass(frozen=True)
class TranscriptPatterns:
    """Compiled transcript markers (see the sections registry)."""

    start: re.Pattern[str]
    footer: re.Pattern[str]
    speaker_line: re.Pattern[str]
    not_speaker: frozenset[str]
    moderator_labels: frozenset[str]
    management_labels: frozenset[str]
    participant_block: re.Pattern[str]
    participant_entry: re.Pattern[str]
    participant_sep: re.Pattern[str]
    analyst_intro: re.Pattern[str]
    qa_start: re.Pattern[str]
    qa_end: re.Pattern[str]
    operator_phrases: tuple[str, ...]
    furniture_lines: frozenset[str]
    furniture_head_lines: int
    furniture_min_share: float
    lost_label_min_words: int


@dataclass(frozen=True)
class NarrativePatterns:
    """Everything M08 reads from the sections registry."""

    results: ResultsPatterns
    transcript: TranscriptPatterns
    text_blocks: dict[str, str]  # XBRL concept -> section


def _c(pattern: str) -> re.Pattern[str]:
    """Compile case-insensitively."""
    return re.compile(pattern, re.IGNORECASE)


def load_narrative_patterns() -> NarrativePatterns:
    """Read and validate the M08 keys of the sections registry -> compiled patterns."""
    raw = load_registry("sections")
    blocks = raw.get("xbrl_text_blocks")
    if not isinstance(blocks, dict) or not blocks:
        raise ConfigError("sections registry: xbrl_text_blocks is missing or empty")
    try:
        sections = _NarrativeSections.model_validate(raw.get("narrative_sections") or {})
        markers = _TranscriptMarkers.model_validate(raw.get("transcript_markers") or {})
        generic = {k: frozenset(v) for k, v in markers.generic_speakers.items()}
        results = ResultsPatterns(
            notes_heading=[_c(p) for p in sections.results_notes.heading],
            auditor_heading=[_c(p) for p in sections.auditor_report.heading],
            auditor_end=[re.compile(p) for p in sections.auditor_report.end],
            not_narrative=[_c(p) for p in sections.not_narrative_head.patterns],
            not_narrative_head_lines=sections.not_narrative_head.head_lines,
            press_start=[_c(p) for p in sections.press_release.start],
            continuation=re.compile(sections.notes_continuation.pattern),
            continuation_min_lines=sections.notes_continuation.min_lines,
        )
        transcript = TranscriptPatterns(
            start=re.compile(markers.start),
            footer=re.compile(markers.footer_line),
            speaker_line=re.compile(markers.speaker_line),
            not_speaker=frozenset(markers.not_speaker),
            moderator_labels=generic.get("moderator", frozenset()),
            management_labels=generic.get("management", frozenset()),
            participant_block=re.compile(markers.participant_block),
            participant_entry=re.compile(markers.participant_entry),
            participant_sep=re.compile(markers.participant_sep),
            analyst_intro=re.compile(markers.analyst_intro),
            qa_start=re.compile(markers.qa_start),
            qa_end=re.compile(markers.qa_end),
            operator_phrases=tuple(r.lower() for r in markers.operator_phrases),
            furniture_lines=frozenset(markers.furniture_lines),
            furniture_head_lines=markers.furniture_head_lines,
            furniture_min_share=markers.furniture_min_share,
            lost_label_min_words=markers.lost_label_min_words,
        )
    except (ValidationError, re.error) as exc:
        raise ConfigError(f"sections registry (narrative keys) is invalid: {exc}") from exc
    return NarrativePatterns(results, transcript, {str(k): str(v) for k, v in blocks.items()})
