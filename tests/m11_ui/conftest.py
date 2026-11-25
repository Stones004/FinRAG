"""Fixtures for M11: a temp database with three companies, facts, filings and review items."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa

from finrag.core.db import init_db, tables, transaction
from finrag.core.types import utc_now_iso

REAL = Path(__file__).resolve().parents[2]

ALPHA, BETA, GAMMA = "INE000A00001", "INE000B00002", "INE000C00003"


@pytest.fixture(autouse=True)
def registries(isolated_repo: Path) -> None:
    """The real registries and prompts where settings.paths points (the app builds the service)."""
    for name in ("taxonomy.yaml", "metrics.yaml", "validation_rules.yaml", "intents.yaml"):
        shutil.copy(REAL / "config" / name, isolated_repo / "config" / name)
    shutil.copytree(REAL / "config" / "prompts", isolated_repo / "config" / "prompts")
    shutil.copytree(REAL / "config" / "vocab", isolated_repo / "config" / "vocab")


def _filing(conn, fid: str, isin: str, ftype: str, period_end: str, status: str = "normalized"):
    conn.execute(
        sa.insert(tables.filings).values(
            filing_id=fid,
            isin=isin,
            filing_type=ftype,
            period_end=period_end,
            sha256=fid * 4,
            status=status,
            created_at=utc_now_iso(),
        )
    )


def _fact(conn, fact_id: str, isin: str, filing_id: str, period_end: str) -> None:
    conn.execute(
        sa.insert(tables.canonical_facts).values(
            fact_id=fact_id,
            isin=isin,
            concept="revenue",
            period_end=period_end,
            period_type="Q",
            basis="consolidated",
            value="1",
            source_filing_id=filing_id,
            is_derived=0,
            created_at=utc_now_iso(),
            source_class="xbrl",
        )
    )


@pytest.fixture
def seeded() -> None:
    """Alpha: facts to 2026-06-30, transcripts to 2026-03-31 (a newer failed one is ignored).
    Beta: facts to 2026-03-31, no transcript. Gamma: nothing. Three open review items of two
    types, one resolved."""
    init_db()
    now = utc_now_iso()
    with transaction() as conn:
        for isin, name in ((ALPHA, "Alpha Ltd"), (BETA, "Beta Ltd"), (GAMMA, "Gamma Ltd")):
            conn.execute(
                sa.insert(tables.entities).values(
                    isin=isin, name=name, status="active", added_on=now, updated_on=now
                )
            )
        _filing(conn, "FIL-a1", ALPHA, "financial_results", "2026-06-30")
        _filing(conn, "FIL-a0", ALPHA, "financial_results", "2026-03-31")
        _filing(conn, "FIL-at1", ALPHA, "earnings_call_transcript", "2026-03-31")
        _filing(conn, "FIL-at2", ALPHA, "earnings_call_transcript", "2026-06-30", "failed")
        _filing(conn, "FIL-b1", BETA, "financial_results", "2026-03-31")
        _fact(conn, "FCT-a1", ALPHA, "FIL-a1", "2026-06-30")
        _fact(conn, "FCT-a0", ALPHA, "FIL-a0", "2026-03-31")
        _fact(conn, "FCT-b1", BETA, "FIL-b1", "2026-03-31")
        for item_type, status in (
            ("unmapped_concept", "open"),
            ("unmapped_concept", "open"),
            ("corporate_event_proposal", "open"),
            ("unmapped_concept", "resolved"),
        ):
            conn.execute(
                sa.insert(tables.review_queue).values(
                    item_type=item_type,
                    payload_json="{}",
                    occurrences=1,
                    status=status,
                    created_at=now,
                    updated_at=now,
                )
            )


@pytest.fixture
def logged() -> str:
    """A query_log row; returns its id."""
    init_db()
    with transaction() as conn:
        conn.execute(
            sa.insert(tables.query_log).values(
                query_id="QRY-1",
                question="how is alpha doing",
                intent_json=json.dumps({"metrics": ["revenue"]}),
                answer="the answer",
                created_at=utc_now_iso(),
            )
        )
    return "QRY-1"
