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


def _make_v1_database() -> None:
    """Write a schema-version-1 database (parsed_facts without dimensions_json) with one row."""
    import sqlite3

    from finrag.core.config import get_settings

    path = get_settings().paths.db
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_version VALUES (1, '2026-10-11T00:00:00+00:00');
            CREATE TABLE parsed_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, filing_id TEXT NOT NULL, concept_raw TEXT,
                label_raw TEXT, period_start TEXT, period_end TEXT, basis TEXT, value TEXT,
                unit_raw TEXT, scale INTEGER, decimals INTEGER, source_loc TEXT, extractor TEXT,
                ocr_backend TEXT, confidence FLOAT
            );
            INSERT INTO parsed_facts (filing_id, concept_raw, value) VALUES ('FIL-x', 'a', '1');
            """
        )


def test_version_1_database_is_upgraded_in_place() -> None:
    _make_v1_database()
    init_db()
    columns = {c["name"] for c in sa.inspect(get_engine()).get_columns("parsed_facts")}
    assert {"dimensions_json", "statement_type"} <= columns
    with transaction() as conn:
        version = conn.execute(sa.select(sa.func.max(tables.schema_version.c.version))).scalar()
        assert version == SCHEMA_VERSION == 3
        row = conn.execute(sa.select(tables.parsed_facts)).one()
    assert (row.filing_id, row.concept_raw, row.value, row.dimensions_json, row.statement_type) == (
        "FIL-x",
        "a",
        "1",
        None,
        None,
    )
    init_db()  # a second call changes nothing
    assert _count(tables.parsed_facts) == 1


def test_version_2_database_gets_statement_type() -> None:
    """A v2 database (dimensions_json, no statement_type) is upgraded; its rows are untouched."""
    import sqlite3

    from finrag.core.config import get_settings

    _make_v1_database()
    with sqlite3.connect(get_settings().paths.db) as conn:
        conn.execute("ALTER TABLE parsed_facts ADD COLUMN dimensions_json TEXT")
        conn.execute("INSERT INTO schema_version VALUES (2, '2026-10-12T00:00:00+00:00')")
    init_db()
    columns = {c["name"] for c in sa.inspect(get_engine()).get_columns("parsed_facts")}
    assert "statement_type" in columns
    with transaction() as conn:
        assert conn.execute(sa.select(sa.func.max(tables.schema_version.c.version))).scalar() == 3
        row = conn.execute(sa.select(tables.parsed_facts)).one()
    assert (row.concept_raw, row.value, row.statement_type) == ("a", "1", None)


def test_database_newer_than_the_code_is_refused() -> None:
    from finrag.core.db import SchemaVersionError

    init_db()
    with transaction() as conn:
        conn.execute(
            sa.insert(tables.schema_version).values(version=SCHEMA_VERSION + 1, applied_at="x")
        )
    with pytest.raises(SchemaVersionError):
        init_db()
