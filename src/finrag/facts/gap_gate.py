"""The gap-fill gate: when a PDF/OCR value may be the current value (M05).

A PDF or OCR fact can be current only where no XBRL fact covers the key (it "fills a gap"), and
only if

  (a) its own confidence is at least `parsing.pdf.min_table_confidence`, and
  (b) its own column - the facts of the same filing, statement, basis and period - passes the
      internal identities of the registry (`gap_fill_gate.identities` of the validation rules:
      revenue + other income = total income, profit before tax - tax (+ associates) = profit,
      total assets = total equity and liabilities).

An opening-balance fact whose PDF column has no known period start is also refused: the date of
such a balance cannot be verified. A fact or column that fails stays stored (canonical_facts is
append-only) but the current view skips it. Each decision is an append-only `validation_results`
row (rule id `gap_fill_gate:<filing_id>:<statement>`, status `gap_fill_rejected` or `pass`) that
lists the rejected facts with the reason, and a rejection that changes what is current also raises
a `gap_fill_rejected` review item. Re-running the gate after new facts arrive adds a new row only
when the decision changed; a later row supersedes an earlier one.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from finrag.core.config import ConfigError, Settings, get_settings, load_registry
from finrag.core.db import tables, transaction
from finrag.core.ids import PREFIX_RUN, new_id
from finrag.core.logging import get_logger
from finrag.core.types import FilingStatus, SourceClass, ValidationStatus, utc_now_iso
from finrag.ingestion import registry
from finrag.normalize.taxonomy import Taxonomy, load_taxonomy

RULE_PREFIX = "gap_fill_gate"
ITEM_TYPE = "gap_fill_rejected"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_COLUMN_IDENTITY = "column_identity_failed"
REASON_OPENING_DATE = "opening_date_unverifiable"
REASON_INPUT_REJECTED = "derived_from_rejected_input"
REASON_BASIS = "basis_mismatch"
NO_STATEMENT = "unknown"
_PDF_CLASSES = (SourceClass.PDF.value, SourceClass.OCR.value)

log = get_logger("facts")


@dataclass(frozen=True)
class GateIdentity:
    """total = sum(parts) + sum(optional parts present) - sum(minus), from the rule registry."""

    id: str
    parts: tuple[str, ...]
    total: str
    optional_parts: tuple[str, ...] = ()
    minus: tuple[str, ...] = ()


@dataclass
class ColumnFact:
    """One PDF/OCR fact of a column, with what the gate needs to judge it."""

    fact_id: str
    concept: str
    value: Decimal
    confidence: float
    parsed_period_start: str | None
    page: int | None = None  # PDF page of the cell (from source_loc "page:row:column")
    would_fill: bool = True  # no XBRL fact covers its key
    reasons: list[str] = field(default_factory=list)


@dataclass
class Column:
    """The facts of one filing, statement, basis and period."""

    isin: str
    filing_id: str
    statement_type: str
    basis: str
    period_start: str
    period_end: str
    period_type: str
    facts: list[ColumnFact] = field(default_factory=list)
    identities: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rule_id(self) -> str:
        """The validation_results rule id that carries this column's decision."""
        return f"{RULE_PREFIX}:{self.filing_id}:{self.statement_type}"

    @property
    def rejected(self) -> list[ColumnFact]:
        """Facts the current view must skip."""
        return [f for f in self.facts if f.reasons]

    @property
    def status(self) -> ValidationStatus:
        """gap_fill_rejected if anything is refused, else pass."""
        return ValidationStatus.GAP_FILL_REJECTED if self.rejected else ValidationStatus.PASS

    @property
    def changes_current_view(self) -> bool:
        """True when a refused fact would otherwise have been the current value of its key."""
        return any(f.would_fill for f in self.rejected)

    def details(self, min_confidence: float) -> dict[str, Any]:
        """The decision as stored in validation_results.details_json."""
        failed = [i["id"] for i in self.identities if i["status"] == ValidationStatus.BLOCK.value]
        return {
            "filing_id": self.filing_id,
            "statement_type": self.statement_type,
            "min_confidence": min_confidence,
            "column_rejected": bool(failed),
            "failed_identities": failed,
            "identities": self.identities,
            "facts_in_column": len(self.facts),
            "rejected_facts": [
                {
                    "fact_id": f.fact_id,
                    "concept": f.concept,
                    "value": format(f.value, "f"),
                    "confidence": round(f.confidence, 4),
                    "reasons": f.reasons,
                    "would_fill": f.would_fill,
                }
                for f in sorted(self.rejected, key=lambda x: x.concept)
            ],
        }


# --- registry ----------------------------------------------------------------------------------


def load_gate_identities() -> list[GateIdentity]:
    """The identities named in `gap_fill_gate.identities`, as defined by the cross_source rule."""
    raw = load_registry("validation_rules")
    gate = raw.get("gap_fill_gate")
    if not isinstance(gate, dict) or not gate.get("identities"):
        raise ConfigError("validation_rules: gap_fill_gate.identities is missing")
    defined: dict[str, dict[str, Any]] = {}
    for rule in raw.get("rules", []):
        if rule.get("type") == "cross_source":
            defined.update({i["id"]: i for i in rule.get("identities", [])})
    out = []
    for name in gate["identities"]:
        if name not in defined:
            raise ConfigError(f"validation_rules: gap_fill_gate names unknown identity '{name}'")
        d = defined[name]
        out.append(
            GateIdentity(
                id=name,
                parts=tuple(d["parts"]),
                total=d["total"],
                optional_parts=tuple(d.get("optional_parts", [])),
                minus=tuple(d.get("minus", [])),
            )
        )
    return out


# --- evaluation --------------------------------------------------------------------------------


def _exceeds(a: Decimal, b: Decimal, settings: Settings) -> bool:
    """True when two rupee values differ by more than BOTH validation tolerances."""
    tol = settings.validation
    diff = abs(a - b)
    larger = max(abs(a), abs(b))
    if not diff or larger == 0:
        return False
    return diff / larger > Decimal(str(tol.relative_tolerance)) and diff > Decimal(
        tol.absolute_tolerance_inr
    )


def check_identity(
    identity: GateIdentity, values: dict[str, Decimal], settings: Settings
) -> dict[str, Any]:
    """One identity on one column -> {id, status: pass|block|skip, ...}. A fail is `block`."""
    required = [*identity.parts, *identity.minus, identity.total]
    missing = [c for c in required if c not in values]
    if missing:
        return {"id": identity.id, "status": ValidationStatus.SKIP.value, "missing": missing}
    present = [c for c in identity.optional_parts if c in values]
    computed = sum((values[c] for c in [*identity.parts, *present]), Decimal(0))
    computed -= sum((values[c] for c in identity.minus), Decimal(0))
    failed = _exceeds(computed, values[identity.total], settings)
    return {
        "id": identity.id,
        "status": (ValidationStatus.BLOCK if failed else ValidationStatus.PASS).value,
        "sum": format(computed, "f"),
        "total": format(values[identity.total], "f"),
        "concepts": [*identity.parts, *present, *identity.minus, identity.total],
    }


def _xbrl_keys(conn: Connection, isin: str) -> set[tuple[str, str, str, str]]:
    """(concept, basis, start, end) of every fact an XBRL fact (or XBRL-only derivation) covers."""
    c = tables.canonical_facts
    rows = conn.execute(
        sa.select(c.c.concept, c.c.basis, c.c.period_start, c.c.period_end, c.c.derivation_json)
        .select_from(c.join(tables.filings, tables.filings.c.filing_id == c.c.source_filing_id))
        .where(
            c.c.isin == isin,
            c.c.source_class.in_((SourceClass.XBRL.value, SourceClass.DERIVED.value)),
            tables.filings.c.status != FilingStatus.QUARANTINED.value,
        )
    )
    keys = set()
    for r in rows:
        if r.derivation_json and not json.loads(r.derivation_json).get("all_xbrl"):
            continue
        keys.add((r.concept, r.basis, r.period_start, r.period_end))
    return keys


def _columns(conn: Connection, isin: str) -> dict[tuple[str, ...], Column]:
    """Every PDF/OCR column of a company, keyed by (filing, statement, basis, start, end)."""
    c, p, f = tables.canonical_facts, tables.parsed_facts, tables.filings
    rows = conn.execute(
        sa.select(
            c.c.fact_id,
            c.c.concept,
            c.c.value,
            c.c.basis,
            c.c.period_start,
            c.c.period_end,
            c.c.period_type,
            c.c.source_filing_id,
            p.c.confidence,
            p.c.statement_type,
            p.c.period_start.label("parsed_start"),
            p.c.source_loc,
        )  # fmt: skip
        .select_from(
            c.join(f, f.c.filing_id == c.c.source_filing_id).outerjoin(
                p, p.c.id == c.c.parsed_fact_id
            )
        )
        .where(
            c.c.isin == isin,
            c.c.source_class.in_(_PDF_CLASSES),
            c.c.is_derived == 0,
            f.c.status != FilingStatus.QUARANTINED.value,
        )
        .order_by(c.c.created_at, c.c.fact_id)
    )
    columns: dict[tuple[str, ...], Column] = {}
    covered = _xbrl_keys(conn, isin)
    for r in rows:
        statement = r.statement_type or NO_STATEMENT
        key = (r.source_filing_id, statement, r.basis, r.period_start or "", r.period_end)
        col = columns.setdefault(
            key,
            Column(
                isin,
                r.source_filing_id,
                statement,
                r.basis,
                r.period_start or r.period_end,
                r.period_end,
                r.period_type,
            ),  # fmt: skip
        )
        col.facts.append(
            ColumnFact(
                r.fact_id,
                r.concept,
                Decimal(r.value),
                float(r.confidence or 0.0),
                r.parsed_start,
                page=int(r.source_loc.split(":")[0]) if r.source_loc else None,
                would_fill=(r.concept, r.basis, r.period_start, r.period_end) not in covered,
            )
        )
    return columns


def judge(
    columns: Iterable[Column],
    identities: list[GateIdentity],
    taxonomy: Taxonomy,
    settings: Settings,
    misbased: Mapping[Any, str] | None = None,
    pages: Mapping[str, int | None] | None = None,
) -> list[Column]:
    """Apply (a), (b), the opening-date and the basis rules to every column (mutated, returned).

    `misbased` is `facts.basis_check.misbased_columns`: a PDF column whose numbers match the other
    basis's XBRL cannot be trusted under its own label.
    """
    minimum = settings.parsing.pdf.min_table_confidence
    opening = taxonomy.opening_names
    misbased = misbased or {}
    columns = list(columns)
    # the other columns printed on a page with a misbased column are under the same wrong label
    bad_pages = {
        (filing, basis, page) for ((filing, _end, page), basis) in misbased if page is not None
    }
    out = []
    for col in columns:
        for fact in col.facts:
            if (col.filing_id, col.basis, fact.page) in bad_pages:
                fact.reasons.append(REASON_BASIS)
        # an opening balance needs the column's period start (the day before it is the date)
        for fact in col.facts:
            if fact.confidence < minimum:
                fact.reasons.append(REASON_LOW_CONFIDENCE)
            if fact.concept in opening and fact.parsed_period_start is None:
                fact.reasons.append(REASON_OPENING_DATE)
        values: dict[str, Decimal] = {}
        for fact in col.facts:  # the latest fact of a concept stands for the column
            values[fact.concept] = fact.value
        col.identities = [check_identity(i, values, settings) for i in identities]
        if any(i["status"] == ValidationStatus.BLOCK.value for i in col.identities):
            for fact in col.facts:
                if REASON_COLUMN_IDENTITY not in fact.reasons:
                    fact.reasons.append(REASON_COLUMN_IDENTITY)
        out.append(col)
    return out


def _misbased(conn: Connection, isin: str, settings: Settings) -> dict[Any, str]:
    """PDF pages of the company (per period end) that carry the wrong basis (facts.basis_check)."""
    from finrag.facts.basis_check import misbased_columns
    from finrag.facts.current_view import load_facts

    c, p = tables.canonical_facts, tables.parsed_facts
    page_of = {
        r.fact_id: int(r.source_loc.split(":")[0])
        for r in conn.execute(
            sa.select(c.c.fact_id, p.c.source_loc)
            .select_from(c.join(p, p.c.id == c.c.parsed_fact_id))
            .where(
                c.c.isin == isin, c.c.source_class.in_(_PDF_CLASSES), p.c.source_loc.is_not(None)
            )
        )
    }
    by_basis: dict[str, list[Any]] = defaultdict(list)
    for fact in load_facts(conn, isin, include_rejected=True):
        by_basis[fact.basis].append(fact)
    return misbased_columns(
        by_basis,
        settings,
        lambda f: (f.source_filing_id, f.period.end, page_of.get(f.fact_id)),
    )


def evaluate(
    isin: str,
    engine: Engine | None = None,
    settings: Settings | None = None,
    taxonomy: Taxonomy | None = None,
) -> list[Column]:
    """The gate's decision for every PDF/OCR column of a company (read-only)."""
    settings = settings or get_settings()
    with transaction(engine) as conn:
        columns = _columns(conn, isin)
        misbased = _misbased(conn, isin, settings)
    judged = judge(
        columns.values(), load_gate_identities(), taxonomy or load_taxonomy(), settings, misbased
    )
    return sorted(judged, key=lambda c: (c.period_end, c.filing_id, c.statement_type, c.basis))


# --- storing and reading decisions -------------------------------------------------------------


@dataclass
class GateReport:
    """What a gate run decided and wrote."""

    isin: str
    columns: int = 0
    rejected_columns: int = 0
    rejected_facts: int = 0
    rows_written: int = 0
    review_items: int = 0
    decisions: list[Column] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        """Counts only, for the CLI and jobs rows."""
        return {
            "columns": self.columns,
            "rejected_columns": self.rejected_columns,
            "rejected_facts": self.rejected_facts,
            "rows_written": self.rows_written,
            "review_items": self.review_items,
        }


def _latest_rows(conn: Connection, isin: str) -> dict[tuple[str, str, str, str], sa.Row]:
    """Newest validation_results row per (rule id, period end, period type, basis)."""
    v = tables.validation_results
    rows = conn.execute(
        sa.select(v).where(v.c.isin == isin, v.c.rule_id.like(f"{RULE_PREFIX}:%")).order_by(v.c.id)
    )
    return {(r.rule_id, r.period_end, r.period_type, r.basis): r for r in rows}


def rejected_fact_ids(conn: Connection, isin: str) -> dict[str, str]:
    """fact_id -> reasons (text) of every fact the latest gate decisions refuse."""
    out: dict[str, str] = {}
    for row in _latest_rows(conn, isin).values():
        if row.status != ValidationStatus.GAP_FILL_REJECTED.value:
            continue
        for item in json.loads(row.details_json).get("rejected_facts", []):
            out[item["fact_id"]] = ",".join(item["reasons"])
    return out


def _same_decision(row: sa.Row | None, details: dict[str, Any], status: ValidationStatus) -> bool:
    """True when the stored newest decision already says this."""
    if row is None:
        return status is ValidationStatus.PASS and not details["rejected_facts"]
    old = json.loads(row.details_json)
    key = lambda d: sorted((f["fact_id"], tuple(f["reasons"])) for f in d["rejected_facts"])  # noqa: E731
    return row.status == status.value and key(old) == key(details)


def run_gate(
    isin: str,
    engine: Engine | None = None,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> GateReport:
    """Evaluate the gate for a company and append the decisions that changed (+ review items)."""
    settings = settings or get_settings()
    decisions = evaluate(isin, engine, settings)
    report = GateReport(isin, columns=len(decisions), decisions=decisions)
    minimum = settings.parsing.pdf.min_table_confidence
    run_id, now = new_id(PREFIX_RUN), utc_now_iso()
    with transaction(engine) as conn:
        latest = _latest_rows(conn, isin)
        for col in decisions:
            report.rejected_columns += bool(col.rejected)
            report.rejected_facts += len(col.rejected)
            details = col.details(minimum)
            previous = latest.get((col.rule_id, col.period_end, col.period_type, col.basis))
            if dry_run or _same_decision(previous, details, col.status):
                continue
            conn.execute(
                sa.insert(tables.validation_results).values(
                    isin=isin,
                    period_end=col.period_end,
                    period_type=col.period_type,
                    basis=col.basis,
                    rule_id=col.rule_id,
                    status=col.status.value,
                    details_json=json.dumps(details, sort_keys=True),
                    run_id=run_id,
                    created_at=now,
                )
            )
            report.rows_written += 1
            if col.changes_current_view:
                registry.queue_review(
                    conn,
                    ITEM_TYPE,
                    {
                        "isin": isin,
                        "filing_id": col.filing_id,
                        "statement_type": col.statement_type,
                        "basis": col.basis,
                        "period_start": col.period_start,
                        "period_end": col.period_end,
                        "failed_identities": details["failed_identities"],
                        "rejected_facts": details["rejected_facts"],
                        "detail": "PDF/OCR values refused as gap-fillers; they stay stored",
                    },
                    {
                        "filing_id": col.filing_id,
                        "statement_type": col.statement_type,
                        "basis": col.basis,
                        "period_start": col.period_start,
                        "period_end": col.period_end,
                    },
                )
                report.review_items += 1
    log.info("gap-fill gate", extra={"event": "gap_gate", "isin": isin, **report.as_dict()})
    return report


def group_by_status(columns: Iterable[Column]) -> dict[str, list[Column]]:
    """Columns grouped by their decision status (for the CLI)."""
    grouped: dict[str, list[Column]] = defaultdict(list)
    for col in columns:
        grouped[col.status.value].append(col)
    return dict(grouped)
