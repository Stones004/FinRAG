"""Convert a parsed value to the concept's canonical unit (rupees, per-share, shares or ratio)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from finrag.normalize.taxonomy import Concept, Taxonomy
from finrag.parsing.models import EXTRACTOR_XBRL, ParsedFact

UNIT_INR = "INR"
PDF_NEGATE = "negate"

REASON_UNIT_MISSING = "unit_missing"
REASON_UNIT_MISMATCH = "unit_mismatch"


@dataclass(frozen=True)
class UnitResult:
    """Converted value, or the reason it cannot be converted."""

    value: Decimal | None
    reason: str | None = None
    detail: str = ""


def to_canonical(fact: ParsedFact, concept: Concept, taxonomy: Taxonomy) -> UnitResult:
    """ParsedFact + concept -> value in the concept's unit (Decimal), or a reason it cannot be."""
    if fact.value is None:
        return UnitResult(None, "nil", "the filing shows no value")
    if fact.extractor == EXTRACTOR_XBRL:
        allowed = taxonomy.xbrl_units.get(concept.unit, ())
        if fact.unit_raw not in allowed:
            detail = f"{fact.unit_raw!r} is not a unit for {concept.unit} ({', '.join(allowed)})"
            return UnitResult(None, REASON_UNIT_MISMATCH, detail)
        return UnitResult(fact.value)  # XBRL values are already full rupees (scale 0)
    value = fact.value
    if concept.unit == UNIT_INR:  # per-share, share counts and ratios are never scaled
        if fact.scale is None:
            return UnitResult(None, REASON_UNIT_MISSING, "the page has no unit line")
        value = value * Decimal(fact.scale)
    return UnitResult(-value if concept.pdf_sign == PDF_NEGATE else value)
