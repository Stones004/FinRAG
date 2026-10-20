"""LLM fallback for weak fields: it may only pick IDs from a shortlist; others are discarded."""

from __future__ import annotations

from dataclasses import dataclass

from finrag.core.logging import get_logger
from finrag.llm.base import LLMClient, LLMUnavailable
from finrag.llm.prompts import load_prompt, render
from finrag.query.intents import Intents
from finrag.query.models import Candidate

log = get_logger("query.classifier")

NONE = "none"
SYSTEM = "You are a careful classifier. You only choose from the lists you are given."


def classify_schema() -> dict:
    """The JSON schema of the classifier reply."""
    return {
        "type": "object",
        "properties": {
            "entity": {"type": "string"},
            "metric": {"type": "string"},
            "confidence": {"enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["entity", "metric", "confidence", "reason"],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class Classified:
    """What the LLM chose (None = nothing valid) and the confidence mapped from its word."""

    entity: str | None
    metric: str | None
    confidence: float
    reason: str


def _lines(candidates: list[Candidate]) -> str:
    """Shortlist -> one line per candidate: 'id: name - description'."""
    if not candidates:
        return "(none)"
    return "\n".join(
        f"{c.id}: {c.name}" + (f" - {c.description.strip()}" if c.description else "")
        for c in candidates
    )


def classify(
    client: LLMClient,
    question: str,
    entities: list[Candidate],
    metrics: list[Candidate],
    intents: Intents,
) -> Classified | None:
    """Question and shortlists -> the LLM's validated choice, or None if it failed or was invalid.

    Each chosen ID must be in its shortlist (an ID outside it is discarded for that field); a reply
    that chooses nothing valid returns None. The LLM never sees more than the shortlists.
    """
    prompt = render(
        load_prompt("classify"),
        {
            "question": question,
            "entity_shortlist": _lines(entities),
            "metric_shortlist": _lines(metrics),
        },
    )
    try:
        reply = client.generate(SYSTEM, prompt, classify_schema())
    except LLMUnavailable as exc:
        log.warning("classifier unavailable", extra={"fields": {"error": str(exc)}})
        return None
    data = reply.data or {}
    entity = data.get("entity")
    metric = data.get("metric")
    entity = entity if entity in {c.id for c in entities} else None
    metric = metric if metric in {c.id for c in metrics} else None
    if entity is None and metric is None:
        log.info("classifier choice discarded", extra={"fields": {"reply": data}})
        return None
    return Classified(
        entity=entity,
        metric=metric,
        confidence=intents.llm_confidence_map[data["confidence"]],
        reason=str(data.get("reason", "")),
    )
