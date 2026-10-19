"""Which PDF/OCR review requests are worth a person's time (M04).

A PDF/OCR label or issue is queued only if its value would actually be used:

* a label the taxonomy does not know (`unmapped_concept`): the value must not be covered by an
  XBRL fact of the same company, basis and period (no concept is known, so "the same key" is the
  same period and basis with the same value, within the PDF's rounding), its confidence must be at
  least `parsing.pdf.min_table_confidence`, and its column must pass the gap-fill gate identities
  (checked on the filing's own candidates);
* a `normalization_issue`: unusable cells (period or name unreadable) are counted, not queued; a
  `low_confidence_sole_source` is the gate's business now; a `duplicate_candidates` item is queued
  only if no XBRL fact covers its key.

Everything else is counted in `normalize report` and in the job stats. Items already open that
this makes unnecessary are closed as `ignored` with the reason, marked `auto_closed` so that a
later run re-evaluates the label instead of ignoring it for good.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import Settings
from finrag.core.db import tables, transaction
from finrag.core.types import FilingFormat, FilingStatus, ValidationStatus
from finrag.facts.gap_gate import GateIdentity, check_identity, load_gate_identities
from finrag.normalize.basis import resolve_basis
from finrag.normalize.models import CanonicalCandidate
from finrag.normalize.taxonomy import Taxonomy
from finrag.parsing.models import EXTRACTOR_XBRL, ParsedFact

# code -> the reason written on a closed item
REASONS = {
    "xbrl_covers_key": "XBRL covers this key",
    "low_confidence": "confidence below the table minimum: the value cannot fill a gap",
    "column_fails_gate": "its column fails the gap-fill gate identities: it cannot fill a gap",
    "gate_owns_low_confidence": "low-confidence PDF/OCR values are judged by the gap-fill gate",
    "unusable_cell": "no usable period or name (a misread column header): counted, not queued",
    "ambiguous_duplicate": "two PDF cells claim this key with different values and both are "
    "dropped, so nothing is used: counted, not queued",
    "garbled_label": "the label has the row's numbers merged into it (OCR): no mapping can match "
    "it and the value is unreliable: counted, not queued",
}
GARBLED = re.compile(r"\b\d+\b")  # a label with a number of its own, outside parentheses
PARENTHESES = re.compile(r"\([^)]*\)")  # row numbering such as "(5-6)" is not a number
UNUSABLE_ISSUES = ("period_misaligned", "period_kind_mismatch", "period_missing", "no_name")

ITEM_UNMAPPED = "unmapped_concept"
ITEM_ISSUE = "normalization_issue"
Key = tuple[str, date, date, str]  # concept, start, end, basis


@dataclass(frozen=True)
class Usability:
    """What the filters need to know about the company's other sources."""

    min_confidence: float
    covered: frozenset[Key]  # keys of the company's XBRL candidates
    xbrl_values: Mapping[tuple[str, str], tuple[Decimal, ...]]  # (basis, period end) -> values
    rounding: Decimal  # the PDF's own rounding, rupees
    identities: tuple[GateIdentity, ...]
    settings: Settings


@dataclass(frozen=True)
class NotNeeded:
    """A review item this run does not need: what it was, how to find it, why."""

    item_type: str
    match: dict[str, object]
    reason: str


@dataclass
class Filtered:
    """The requests that survive, and the ones dropped with their reasons."""

    dropped: list[NotNeeded] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def xbrl_value_index(
    isin: str, engine: Engine | None = None
) -> dict[tuple[str, str], tuple[Decimal, ...]]:
    """(basis, period end) -> every entity-total XBRL value (rupees) of the company's filings."""
    p, f = tables.parsed_facts, tables.filings
    with transaction(engine) as conn:
        rows = conn.execute(
            sa.select(f.c.basis, p.c.period_end, p.c.value)
            .select_from(p.join(f, f.c.filing_id == p.c.filing_id))
            .where(
                f.c.isin == isin,
                f.c.format == FilingFormat.XBRL.value,
                f.c.status.in_([FilingStatus.PARSED.value, FilingStatus.NORMALIZED.value]),
                p.c.dimensions_json.is_(None),
                p.c.value.is_not(None),
            )
        )
        index: dict[tuple[str, str], set[Decimal]] = defaultdict(set)
        for row in rows:
            try:
                index[(row.basis, row.period_end)].add(Decimal(row.value))
            except ArithmeticError:
                continue
    return {k: tuple(v) for k, v in index.items()}


def build_usability(
    isin: str,
    covered: frozenset[Key],
    settings: Settings,
    engine: Engine | None = None,
) -> Usability:
    """Collect the company-level inputs of the filters."""
    return Usability(
        min_confidence=settings.parsing.pdf.min_table_confidence,
        covered=covered,
        xbrl_values=xbrl_value_index(isin, engine),
        rounding=Decimal(settings.validation.cross_source.rounding_inr),
        identities=tuple(load_gate_identities()),
        settings=settings,
    )


def _covered_by_value(fact: ParsedFact, basis: str, usable: Usability) -> bool:
    """True if an XBRL fact of the same basis and period end has this value (rupees)."""
    if fact.value is None or not fact.scale or fact.period_end is None:
        return False
    rupees = fact.value * fact.scale
    values = usable.xbrl_values.get((basis, fact.period_end.isoformat()), ())
    return any(abs(v - rupees) <= usable.rounding for v in values)


def _column_fails(
    fact: ParsedFact,
    basis: str,
    candidates: list[CanonicalCandidate],
    taxonomy: Taxonomy,
    usable: Usability,
) -> bool:
    """True if the fact's column (statement, basis, period) breaks a gate identity."""
    values: dict[str, Decimal] = {}
    for cand in candidates:
        if cand.extractor == EXTRACTOR_XBRL or cand.basis != basis:
            continue
        if taxonomy.concepts[cand.concept].statement != fact.statement_type:
            continue
        if cand.period_end != fact.period_end:
            continue
        if fact.period_start is not None and cand.period_start != fact.period_start:
            continue
        values[cand.concept] = cand.value
    return any(
        check_identity(identity, values, usable.settings)["status"] == ValidationStatus.BLOCK.value
        for identity in usable.identities
    )


def unmapped_reason(
    fact: ParsedFact,
    family_basis: str | None,
    candidates: list[CanonicalCandidate],
    taxonomy: Taxonomy,
    usable: Usability,
) -> str | None:
    """Reason code why an unmapped PDF/OCR label's value would never be used, or None."""
    if fact.extractor == EXTRACTOR_XBRL:
        return None
    if fact.confidence < usable.min_confidence:
        return "low_confidence"
    if fact.label_raw and GARBLED.search(PARENTHESES.sub(" ", fact.label_raw)):
        return "garbled_label"
    basis = resolve_basis(fact.basis, family_basis).value
    if _covered_by_value(fact, basis, usable):
        return "xbrl_covers_key"
    if _column_fails(fact, basis, candidates, taxonomy, usable):
        return "column_fails_gate"
    return None
