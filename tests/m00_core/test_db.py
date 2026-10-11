import pytest
import sqlalchemy as sa

from finrag.core.db import SCHEMA_VERSION, get_engine, init_db, tables, transaction

SPEC_TABLES = {
    "entities", "entity_aliases", "corporate_events", "filings", "parsed_facts",
    "canonical_facts", "review_queue", "restatements", "validation_results", "metric_values",
    "chunks", "query_log", "jobs", "alerts", "signals",
}  # fmt: skip


def _count(table: sa.Table) -> int:
    with transaction() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


def test_init_creates_all_spec_tables_and_version() -> None:
    init_db()
    names = set(sa.inspect(get_engine()).get_table_names())
    assert SPEC_TABLES | {"schema_version"} <= names
    with transaction() as conn:
        assert conn.execute(sa.select(tables.schema_version.c.version)).scalar() == SCHEMA_VERSION


def test_init_is_idempotent_and_keeps_data() -> None:
    init_db()
    with transaction() as conn:
        conn.execute(sa.insert(tables.entities).values(isin="INE000A01010", name="Test"))
    init_db()
    assert _count(tables.entities) == 1
    assert _count(tables.schema_version) == 1


def test_transaction_rolls_back_on_error() -> None:
    init_db()
    with pytest.raises(RuntimeError), transaction() as conn:
        conn.execute(sa.insert(tables.entities).values(isin="INE000A01010"))
        raise RuntimeError("boom")
    assert _count(tables.entities) == 0


def test_foreign_keys_enforced_and_sha_unique() -> None:
    init_db()
    row = {"filing_id": "FIL-aaaaaaaaaaaa", "isin": "INE000A01010", "sha256": "x"}
    with pytest.raises(sa.exc.IntegrityError), transaction() as conn:
        conn.execute(sa.insert(tables.filings).values(**row))
    with transaction() as conn:
        conn.execute(sa.insert(tables.entities).values(isin="INE000A01010"))
        conn.execute(sa.insert(tables.filings).values(**row))
    with pytest.raises(sa.exc.IntegrityError), transaction() as conn:
        conn.execute(sa.insert(tables.filings).values(**{**row, "filing_id": "FIL-bbbbbbbbbbbb"}))
