"""SQLite schema (MASTER_BUILD §7), engine access and transactions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from finrag.core.config import get_settings
from finrag.core.types import utc_now_iso

SCHEMA_VERSION = 1

metadata = sa.MetaData()


def _table(name: str, *items: Any) -> sa.Table:
    """Create a table on the shared metadata."""
    return sa.Table(name, metadata, *items)


def _text(name: str, **kw: Any) -> sa.Column[Any]:
    return sa.Column(name, sa.Text, **kw)


def _int(name: str, **kw: Any) -> sa.Column[Any]:
    return sa.Column(name, sa.Integer, **kw)


def _id_pk() -> sa.Column[Any]:
    return sa.Column("id", sa.Integer, primary_key=True, autoincrement=True)


_table(
    "schema_version",
    _int("version", primary_key=True, autoincrement=False),
    _text("applied_at", nullable=False),
)

# --- 7.1 reference ---
_table(
    "entities",
    _text("isin", primary_key=True),
    _text("name"),
    _text("nse_symbol"),
    _text("bse_code"),
    _text("sector_type"),
    _text("default_basis"),
    _text("status"),
    _text("added_on"),
    _text("updated_on"),
)
_table(
    "entity_aliases",
    _id_pk(),
    _text("isin", nullable=False),
    _text("alias", nullable=False),
    _text("alias_type"),  # symbol|code|name|user
    _text("valid_from"),
    _text("valid_to"),
    sa.ForeignKeyConstraint(["isin"], ["entities.isin"]),
)
_table(
    "corporate_events",
    _id_pk(),
    _text("isin", nullable=False),
    _text("event_type"),  # rename|merger|demerger|delisting|basis_change
    _text("event_date"),
    _text("details"),
    _text("source"),
    sa.ForeignKeyConstraint(["isin"], ["entities.isin"]),
)

# --- 7.2 ingestion ---
_table(
    "filings",
    _text("filing_id", primary_key=True),
    _text("isin", nullable=False),
    _text("source"),
    _text("filing_type"),  # financial_results|annual_report|earnings_call_transcript
    _text("submission"),  # original|revision
    _int("revision_no"),
    _text("taxonomy_family"),  # indas|banking|other
    _text("basis"),
    _text("period_start"),
    _text("period_end"),
    _text("period_type"),
    _text("filed_on"),
    _text("source_url"),
    _text("raw_path"),
    _text("sha256", nullable=False, unique=True),
    _text("format"),  # xbrl|pdf
    _text("status"),  # fetched|parsed|normalized|failed
    _text("error"),
    _text("created_at"),
    sa.ForeignKeyConstraint(["isin"], ["entities.isin"]),
    sa.Index("ix_filings_isin_period", "isin", "period_end"),
)

# --- 7.3 facts ---
_table(
    "parsed_facts",
    _id_pk(),
    _text("filing_id", nullable=False),
    _text("concept_raw"),
    _text("label_raw"),
    _text("period_start"),
    _text("period_end"),
    _text("basis"),
    _text("value"),
    _text("unit_raw"),
    _int("scale"),
    _int("decimals"),
    _text("source_loc"),
    _text("extractor"),  # xbrl|pdf_table|ocr
    _text("ocr_backend"),  # tesseract|chandra; only when extractor = ocr
    sa.Column("confidence", sa.Float),
    sa.ForeignKeyConstraint(["filing_id"], ["filings.filing_id"]),
    sa.Index("ix_parsed_facts_filing", "filing_id"),
)
_table(
    "canonical_facts",  # append-only
    _text("fact_id", primary_key=True),
    _text("isin", nullable=False),
    _text("concept", nullable=False),
    _text("period_start"),
    _text("period_end", nullable=False),
    _text("period_type"),
    _text("basis", nullable=False),
    _text("value", nullable=False),
    _text("unit"),
    _text("source_filing_id", nullable=False),
    _int("parsed_fact_id"),
    _text("mapping_method"),  # standard|label|manual
    _int("is_derived"),
    _text("derivation_json"),
    _text("filed_on"),
    _text("created_at"),
    sa.ForeignKeyConstraint(["source_filing_id"], ["filings.filing_id"]),
    sa.ForeignKeyConstraint(["parsed_fact_id"], ["parsed_facts.id"]),
    sa.Index("ix_canonical_facts_lookup", "isin", "concept", "period_end", "basis"),
)
_table(
    "review_queue",
    _id_pk(),
    # unmapped_concept|ambiguous_basis|low_confidence_table|entity_ambiguous|transcript_link_only
    _text("item_type"),
    _text("payload_json"),
    _int("occurrences"),
    _text("status"),  # open|resolved|ignored
    _text("resolution_json"),
    _text("created_at"),
    _text("updated_at"),
)
_table(
    "restatements",
    _id_pk(),
    _text("isin"),
    _text("concept"),
    _text("period_start"),
    _text("period_end"),
    _text("basis"),
    _text("old_fact_id"),
    _text("new_fact_id"),
    _text("abs_diff"),
    _text("rel_diff"),
    _int("flagged"),
    _text("created_at"),
)

# --- 7.4 validation and metrics ---
_table(
    "validation_results",
    _id_pk(),
    _text("isin"),
    _text("period_end"),
    _text("period_type"),
    _text("basis"),
    _text("rule_id"),
    _text("status"),  # pass|warn|block|skip
    _text("details_json"),
    _text("run_id"),
    _text("created_at"),
)
_table(
    "metric_values",
    _text("metric_value_id", primary_key=True),
    _text("isin"),
    _text("metric_id"),
    _text("formula_version"),
    _text("period_start"),
    _text("period_end"),
    _text("period_type"),
    _text("basis"),
    _text("value"),  # NULL when not computed
    _text("status"),  # computed|not_computed
    _text("reason"),
    _text("input_fact_ids_json"),
    _text("run_id"),
    _text("created_at"),
    sa.Index("ix_metric_values_lookup", "isin", "metric_id", "period_end"),
)

# --- 7.5 narrative ---
_table(
    "chunks",
    _text("chunk_id", primary_key=True),
    _text("filing_id", nullable=False),
    _text("isin"),
    _text("fiscal_year"),
    _text("section"),
    _text("source_type"),  # annual_report|transcript|results
    _text("speaker"),
    _text("speaker_role"),  # management|analyst|moderator
    _int("page"),
    _int("seq"),
    _text("text"),
    _int("token_count"),
    _text("created_at"),
    sa.ForeignKeyConstraint(["filing_id"], ["filings.filing_id"]),
    sa.Index("ix_chunks_isin_fy", "isin", "fiscal_year"),
)

# --- 7.6 query and operations ---
_table(
    "query_log",
    _text("query_id", primary_key=True),
    _text("question"),
    _text("intent_json"),
    _text("plan_json"),
    _text("retrieved_ids_json"),
    _text("prompt_version"),
    _text("model"),
    _text("answer"),
    _text("cited_ids_json"),
    _text("postcheck_json"),
    _text("user_feedback"),
    _text("created_at"),
)
_table(
    "jobs",
    _text("job_id", primary_key=True),
    _text("job_type"),
    _text("isin"),
    _text("status"),
    _text("started_at"),
    _text("finished_at"),
    _text("error"),
    _text("stats_json"),
)
_table(
    "alerts",
    _text("alert_id", primary_key=True),
    _text("isin"),
    _text("alert_type"),
    _text("message"),
    _text("filing_id"),
    _text("sent_channels_json"),
    _text("created_at"),
)

# --- 7.7 reserved (deferred) ---
_table(
    "signals",
    _text("signal_id", primary_key=True),
    _text("isin"),
    _text("signal_type"),
    _text("period_end"),
    _text("value"),
    _text("severity"),
    _text("evidence_json"),
    _text("run_id"),
    _text("created_at"),
)

tables = SimpleNamespace(**dict(metadata.tables))


class SchemaVersionError(Exception):
    """Raised when an existing database has a different schema version than the code."""


_engines: dict[str, Engine] = {}


def _make_engine(db_path: Path) -> Engine:
    """DB file path -> SQLite engine with foreign keys enforced."""
    engine = sa.create_engine(sa.engine.URL.create("sqlite", database=str(db_path)))

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def get_engine() -> Engine:
    """Return the (cached) engine for settings.paths.db."""
    db_path = get_settings().paths.db
    key = str(db_path)
    if key not in _engines:
        _engines[key] = _make_engine(db_path)
    return _engines[key]


def reset_engines() -> None:
    """Dispose and forget all cached engines (used by tests)."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()


def init_db(engine: Engine | None = None) -> None:
    """Create any missing tables and record the schema version; never alters existing data."""
    if engine is None:
        get_settings().paths.db.parent.mkdir(parents=True, exist_ok=True)
        engine = get_engine()
    metadata.create_all(engine)
    with engine.begin() as conn:
        current = conn.execute(sa.select(sa.func.max(tables.schema_version.c.version))).scalar()
        if current is None:
            conn.execute(
                sa.insert(tables.schema_version).values(
                    version=SCHEMA_VERSION, applied_at=utc_now_iso()
                )
            )
        elif current != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema version is {current}, code expects {SCHEMA_VERSION}"
            )


@contextmanager
def transaction(engine: Engine | None = None) -> Iterator[Connection]:
    """Yield a connection inside a transaction: commit on success, roll back on error."""
    with (engine or get_engine()).begin() as conn:
        yield conn
