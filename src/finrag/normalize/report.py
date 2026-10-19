"""Mapping coverage report: how much of an ISIN's parsed facts map, by statement and reason."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.types import FilingFormat, FilingStatus
from finrag.normalize import review_queue as queue
from finrag.normalize.mapper import Mapper
from finrag.normalize.runner import (
    NormalizeOutcome,
    _context,
    apply_usability,
    entity_sector,
    load_rows,
    normalize_facts,
    xbrl_keys,
)
from finrag.normalize.taxonomy import Taxonomy, load_taxonomy
from finrag.normalize.usable import build_usability

STATEMENTS = ("profit_and_loss", "balance_sheet", "cash_flow", "other")


@dataclass
class CoverageReport:
    """Counts for one ISIN over its parsed or normalized filings."""

    isin: str
    filings: int = 0
    considered: int = 0
    dimensioned: int = 0
    nil: int = 0
    mapped_by_statement: Counter[str] = field(default_factory=Counter)
    mapped_by_method: Counter[str] = field(default_factory=Counter)
    known_by_reason: Counter[str] = field(default_factory=Counter)
    not_applicable: int = 0
    placeholder_zero: int = 0
    unmapped: Counter[str] = field(default_factory=Counter)  # key -> facts
    issues: Counter[str] = field(default_factory=Counter)
    not_queued: Counter[str] = field(default_factory=Counter)  # PDF/OCR requests nobody needs

    def add(self, out: NormalizeOutcome) -> None:
        """Add one filing's outcome."""
        self.filings += 1
        self.considered += out.considered
        self.dimensioned += out.dimensioned
        self.nil += out.nil
        self.mapped_by_statement.update(out.mapped_by_statement)
        self.mapped_by_method.update(out.mapped_by_method)
        self.known_by_reason.update(out.known_by_reason)
        self.not_applicable += out.not_applicable
        self.placeholder_zero += out.placeholder_zero
        self.unmapped.update(u.outcome.key for u in out.unmapped)
        self.issues.update(i.reason for i in out.issues)
        self.not_queued.update(out.not_queued)
        if out.basis_unknown:
            self.issues["basis_unknown"] += out.basis_unknown


def build_report(
    isin: str,
    engine: Engine | None = None,
    settings: Settings | None = None,
    taxonomy: Taxonomy | None = None,
) -> CoverageReport:
    """ISIN -> coverage over its parsed/normalized filings (read-only; no review items written)."""
    settings = settings or get_settings()
    min_confidence = settings.parsing.pdf.min_table_confidence
    mapper = Mapper(taxonomy or load_taxonomy(), queue.load_resolutions(engine))
    filings = tables.filings
    with transaction(engine) as conn:
        found = conn.execute(
            sa.select(filings)
            .where(
                filings.c.isin == isin,
                filings.c.status.in_([FilingStatus.PARSED.value, FilingStatus.NORMALIZED.value]),
            )
            .order_by(filings.c.period_end, filings.c.filing_id)
        ).all()
    sector = entity_sector(isin, engine)
    covered = xbrl_keys(isin, mapper, min_confidence, engine)
    usable = build_usability(isin, covered, settings, engine)
    report = CoverageReport(isin)
    for filing in found:
        rows = load_rows(filing.filing_id, engine)
        if not rows:
            continue  # annual reports and transcripts carry no facts
        ctx = _context(filing, sector, mapper.taxonomy)
        is_xbrl = filing.format == FilingFormat.XBRL.value
        outcome = normalize_facts(
            rows, ctx, mapper, min_confidence, frozenset() if is_xbrl else covered
        )
        if not is_xbrl:
            apply_usability(outcome, ctx, mapper.taxonomy, usable)
        report.add(outcome)
    return report


def format_report(report: CoverageReport) -> list[str]:
    """CoverageReport -> printable lines (mapped by statement, known-unmapped by reason, gaps)."""
    lines = [
        f"{report.isin}: {report.filings} filings with facts, {report.considered} entity-total "
        f"facts considered ({report.dimensioned} dimensioned and {report.nil} nil skipped)",
        "",
        f"{'statement':<18}{'mapped':>8}{'% of considered':>18}",
    ]
    for statement in STATEMENTS:
        count = report.mapped_by_statement.get(statement, 0)
        share = 100 * count / report.considered if report.considered else 0
        lines.append(f"{statement:<18}{count:>8}{share:>17.1f}%")
    mapped = sum(report.mapped_by_statement.values())
    share = 100 * mapped / report.considered if report.considered else 0
    lines.append(f"{'all mapped':<18}{mapped:>8}{share:>17.1f}%")
    methods = ", ".join(f"{k} {v}" for k, v in sorted(report.mapped_by_method.items())) or "none"
    lines += ["", f"mapped by method: {methods}", "", "known unmapped, by reason:"]
    for reason, count in sorted(report.known_by_reason.items()):
        lines.append(f"  {reason:<40}{count:>6}")
    if not report.known_by_reason:
        lines.append("  none")
    lines.append(f"not applicable to the sector of the entity: {report.not_applicable}")
    lines.append(f"reported 0 meaning not disclosed (not stored): {report.placeholder_zero}")
    lines.append(f"unmapped (would be queued): {sum(report.unmapped.values())}")
    for key, count in report.unmapped.most_common():
        lines.append(f"  {key}  x{count}")
    if report.issues:
        issues = ", ".join(f"{k} {v}" for k, v in sorted(report.issues.items()))
        lines.append(f"issues (would be queued): {issues}")
    if report.not_queued:
        lines.append("PDF/OCR requests nobody needs (counted, not queued):")
        for reason, count in sorted(report.not_queued.items()):
            lines.append(f"  {reason:<48}{count:>6}")
    return lines
