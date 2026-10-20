"""Plain types for the fact store: a stored fact, its ordering and its source precedence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from finrag.core.types import Period, SourceClass

CHAIN_XBRL = "xbrl"
CHAIN_PDF = "pdf"
CHAIN_DERIVED = "derived"


@dataclass(frozen=True)
class Fact:
    """One row of canonical_facts plus the filing facts that order it (date, revision)."""

    fact_id: str
    isin: str
    concept: str
    period: Period
    basis: str
    value: Decimal
    unit: str
    source_filing_id: str
    parsed_fact_id: int | None
    mapping_method: str | None
    is_derived: bool
    derivation: dict[str, Any] | None
    stored_filed_on: str | None  # as written; the filing's own value (below) is what orders facts
    created_at: str
    source_class: SourceClass
    filed_on: str | None  # the source filing's current filed_on
    revision_no: int
    confidence: float | None  # of the parsed fact (None for derived facts)
    gap_rejected: bool = False  # refused by the gap-fill gate: stored, never current
    gap_reason: str | None = None
    filing_period_end: str | None = None  # the period end of the filing that printed this fact

    @property
    def key(self) -> tuple[str, str, date, date, str]:
        """Identity a restatement is judged on: (isin, concept, start, end, basis)."""
        return (self.isin, self.concept, self.period.start, self.period.end, self.basis)

    @property
    def all_inputs_xbrl(self) -> bool:
        """True for a derived fact whose inputs were all XBRL facts."""
        return bool(self.derivation and self.derivation.get("all_xbrl"))

    @property
    def xbrl_class(self) -> bool:
        """True for an XBRL fact, or a derived fact computed only from XBRL facts."""
        if self.source_class is SourceClass.XBRL:
            return True
        return self.source_class is SourceClass.DERIVED and self.all_inputs_xbrl

    @property
    def chain(self) -> str:
        """Which history this fact belongs to: restatements are judged within one chain."""
        if self.source_class is SourceClass.DERIVED:
            return CHAIN_DERIVED
        return CHAIN_XBRL if self.source_class is SourceClass.XBRL else CHAIN_PDF

    @property
    def is_pdf_comparative(self) -> bool:
        """True for a native-text PDF column of an earlier period than the filing's own.

        The Q1 FY26 column of the Q1 FY27 results is one. A scanned (OCR) column never is: its
        digits are too uncertain to stand for what a company later restated, and a column that ends
        with the filing's own period (a half-year in a Q2 filing) is the company's current report.
        """
        return (
            self.source_class is SourceClass.PDF
            and self.filing_period_end is not None
            and self.period.end.isoformat() < self.filing_period_end
        )

    @property
    def rank(self) -> int:
        """Source precedence, lower wins: XBRL, XBRL-only derived, PDF, PDF comparative, OCR, other
        derived. A comparative is a later filing's second printing of a number: it never beats the
        number printed by the filing of its own period (in XBRL or PDF), it only fills a gap."""
        if self.source_class is SourceClass.XBRL:
            return 0
        if self.source_class is SourceClass.DERIVED:
            return 1 if self.all_inputs_xbrl else 5
        if self.source_class is SourceClass.OCR:
            return 4
        return 3 if self.is_pdf_comparative else 2

    @property
    def known_on(self) -> str:
        """Date (ISO) from which this fact was known: filing date, else the day it was stored."""
        return (self.filed_on or self.created_at)[:10]

    @property
    def order(self) -> tuple[str, int, str]:
        """Recency: filing date (dates only, as granularities differ), revision_no, created_at."""
        return ((self.filed_on or "")[:10], self.revision_no, self.created_at)
