"""Plain data types produced by the parsers and consumed by the runner."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

EXTRACTOR_XBRL = "xbrl"
EXTRACTOR_TABLE = "pdf_table"
EXTRACTOR_OCR = "ocr"


@dataclass(frozen=True)
class Dimension:
    """One axis/member pair of an XBRL context; `typed` members carry the child element text."""

    axis: str
    member: str
    typed: bool = False

    def as_json(self) -> dict[str, str | bool]:
        """Dimension -> the JSON object stored in parsed_facts.dimensions_json."""
        data: dict[str, str | bool] = {"axis": self.axis, "member": self.member}
        if self.typed:
            data["typed"] = True
        return data


def dimensions_json(dimensions: tuple[Dimension, ...]) -> str | None:
    """Dimensions -> JSON text (sorted by axis), or None for an entity total."""
    if not dimensions:
        return None
    ordered = sorted(dimensions, key=lambda d: (d.axis, d.member))
    return json.dumps([d.as_json() for d in ordered], sort_keys=True)


@dataclass(frozen=True)
class ParsedFact:
    """One number exactly as reported, with where it came from (mirrors parsed_facts)."""

    value: Decimal | None  # None: the filing shows a dash (nil)
    concept_raw: str | None = None
    label_raw: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    basis: str = "unknown"
    unit_raw: str | None = None
    scale: int | None = 0  # multiplier into rupees from the unit line (None: no unit line); XBRL 0
    decimals: int | None = None
    source_loc: str | None = None
    dimensions: tuple[Dimension, ...] = ()
    extractor: str = EXTRACTOR_XBRL
    ocr_backend: str | None = None
    confidence: float = 1.0
    statement_type: str | None = None  # PDF/OCR: statement of the page (page classifier)


@dataclass(frozen=True)
class TextFact:
    """A non-numeric XBRL fact (text block, metadata element), kept apart from parsed_facts."""

    concept_raw: str
    context_ref: str
    period_start: date | None
    period_end: date | None
    value: str
    dimensions: tuple[Dimension, ...] = ()

    def as_json(self) -> dict[str, object]:
        """TextFact -> one JSON-lines record."""
        return {
            "concept": self.concept_raw,
            "context": self.context_ref,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "dimensions": [d.as_json() for d in self.dimensions],
            "value": self.value,
        }


@dataclass
class XbrlResult:
    """Everything read from one XBRL instance."""

    facts: list[ParsedFact] = field(default_factory=list)
    text_facts: list[TextFact] = field(default_factory=list)
