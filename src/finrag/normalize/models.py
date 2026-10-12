"""Plain data types shared by the normalizer and handed to M05."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

METHOD_STANDARD = "standard"
METHOD_LABEL = "label"
METHOD_MANUAL = "manual"


@dataclass(frozen=True)
class CanonicalCandidate:
    """One normalized fact offered to M05 (extractor and confidence drive source precedence)."""

    isin: str
    filing_id: str
    parsed_fact_id: int
    concept: str
    period_start: date
    period_end: date
    period_type: str
    basis: str
    value: Decimal
    unit: str
    mapping_method: str
    extractor: str
    ocr_backend: str | None
    confidence: float
    filed_on: str | None
    revision_no: int = 0  # of the source filing; orders a revision after its original

    @property
    def key(self) -> tuple[str, date, date, str]:
        """The identity M05 compares candidates on: (concept, period start, period end, basis)."""
        return (self.concept, self.period_start, self.period_end, self.basis)


class CandidateWriter(Protocol):
    """What M04 needs from M05: `facts.writer.write_candidates` satisfies this."""

    def __call__(self, candidates: Sequence[CanonicalCandidate]) -> object:
        """Persist the candidates (append-only) and return the writer's own stats."""
        ...
