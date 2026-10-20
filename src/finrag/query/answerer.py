"""Draft of the explanation: the LLM is asked only to explain the figures from the passages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from finrag.core.logging import get_logger
from finrag.core.types import Period, PeriodType
from finrag.llm.base import LLMClient, LLMInvalidOutput, LLMResponse, LLMUnavailable
from finrag.llm.prompts import load_prompt, render
from finrag.narrative.search import SearchHit
from finrag.query.claims import CLAIMS_SCHEMA, Claim, parse_claims
from finrag.query.intents import Intents
from finrag.query.models import Evidence, Intent, Plan
from finrag.query.numbers_block import NumbersBlock
from finrag.query.period_resolver import describe_period
from finrag.query.postcheck import NO_CHUNKS, NO_GUIDANCE

log = get_logger("query.answerer")

SYSTEM = "You explain figures that a program has calculated, using only the passages you are given."

NOT_NEEDED = "not_needed"
UNAVAILABLE_SOURCE = "unavailable_source"
NO_CHUNKS_FOUND = "no_chunks"
LLM_DOWN = "llm_unavailable"
LLM_DRAFT = "llm"
LLM_UNREADABLE = "llm_unreadable"

NO_CHUNKS_NOTE = "No passage from the indexed filings was usable for this question."
NO_TRANSCRIPT_NOTE = (
    "No earnings-call transcript was available for this period; "
    "the explanation uses the results text of the filing."
)
NO_USABLE_PASSAGE_NOTE = (
    "No earnings-call transcript was available for this period, and no usable passage was "
    "found in the results text of the filing."
)
NO_FIGURES = "(this question asks for no figures)"
LLM_UNREADABLE_NOTE = (
    "The language model's reply could not be read as claims, so only the figures are shown."
)


@dataclass
class Draft:
    """The explanation before the post-check, and why it is what it is."""

    status: str
    text: str | None = None
    claims: list[Claim] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    response: LLMResponse | None = None
    prompt: str | None = None


class Answerer:
    """Asks the LLM for an explanation only when the question needs one and passages exist."""

    def __init__(self, llm: LLMClient | None, intents: Intents) -> None:
        self.llm = llm
        self.intents = intents

    def _unavailable_note(self, intent: Intent) -> str | None:
        """Sources the question needs that are not indexed in v1 -> the sentence saying so."""
        if not intent.unavailable_sources:
            return None
        names = [self.intents.unavailable_names.get(s, s) for s in intent.unavailable_sources]
        listed = " and ".join(names)
        return (
            f"The {listed} would be in the annual report, which is not indexed in this version, "
            "so I cannot answer from it."
        )

    def draft(self, intent: Intent, plan: Plan, evidence: Evidence, block: NumbersBlock) -> Draft:
        """Intent, evidence and numbers block -> Draft (the LLM is called only if it must be)."""
        if intent.question_type not in ("explanation", "narrative"):
            return Draft(NOT_NEEDED)
        notes: list[str] = []
        unavailable = self._unavailable_note(intent)
        if unavailable:
            notes.append(unavailable)
        if not evidence.chunks:
            if unavailable and intent.question_type == "narrative":
                return Draft(UNAVAILABLE_SOURCE, None, notes=notes)
            if evidence.transcripts_missing:
                notes.append("No earnings-call transcript is indexed for this period.")
            notes.append(NO_CHUNKS_NOTE)
            return Draft(
                NO_CHUNKS_FOUND, NO_GUIDANCE if intent.forward_looking else NO_CHUNKS, notes=notes
            )
        if evidence.transcripts_missing:
            notes.append(NO_TRANSCRIPT_NOTE)
        prompt = render(
            load_prompt("guidance" if intent.forward_looking else "explain"),
            {
                "question": intent.question,
                "period": ", ".join(describe_period(p) for p in intent.periods),
                "basis": intent.basis or "consolidated and standalone",
                "numbers_block": block.text or NO_FIGURES,
                "chunks": self._chunks(evidence),
            },
        )
        if self.llm is None:
            notes.append("The language model is not available, so only the figures are shown.")
            return Draft(LLM_DOWN, None, notes=notes, prompt=prompt)
        try:
            response = self.llm.generate(SYSTEM, prompt, CLAIMS_SCHEMA)
        except LLMInvalidOutput as exc:
            log.warning("llm output unreadable", extra={"fields": {"error": str(exc)}})
            notes.append(LLM_UNREADABLE_NOTE)
            return Draft(LLM_UNREADABLE, None, notes=notes, prompt=prompt)
        except LLMUnavailable as exc:
            log.warning("llm unavailable", extra={"fields": {"error": str(exc)}})
            notes.append("The language model is not available, so only the figures are shown.")
            return Draft(LLM_DOWN, None, notes=notes, prompt=prompt)
        return Draft(LLM_DRAFT, response.text, parse_claims(response.data), notes, response, prompt)

    @staticmethod
    def _chunks(evidence: Evidence) -> str:
        """Chunks -> '[N1] (what it is, its period, who speaks) text', one block per passage."""
        by_id = {h.chunk_id: h for h in evidence.chunks}
        return "\n\n".join(
            f"[{alias}] ({passage_label(by_id[cid])}) {by_id[cid].text}"
            for alias, cid in evidence.chunk_aliases.items()
        )


def passage_label(hit: SearchHit) -> str:
    """A passage -> where it is from, its period and its speaker (the prompt's passage label)."""
    when = hit.period_label or "period not identified"
    try:
        if hit.period_start and hit.period_end and hit.period_type:
            when = describe_period(
                Period(
                    date.fromisoformat(hit.period_start),
                    date.fromisoformat(hit.period_end),
                    PeriodType(hit.period_type),
                )
            )
    except (ValueError, KeyError):
        pass
    if hit.source_type == "transcript":
        firm = f", {hit.firm}" if hit.firm else ""
        return (
            f"earnings call; period {when}; speaker {hit.speaker or 'not identified'}; "
            f"role {hit.speaker_role or 'not identified'}{firm}"
        )
    section = (hit.section or "").replace("_", " ")
    page = f"page {hit.page}" if hit.page is not None else "XBRL text block"
    return f"results filing, {section}; period {when}; {page}; no speaker"
