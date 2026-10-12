"""Fixtures for M01: a synthetic securities master and helpers to write config files."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from finrag.core.db import init_db, tables, transaction
from finrag.watchlist.master import SecuritiesMaster, SecurityRecord
from finrag.watchlist.registry import SyncReport, sync_watchlist
from finrag.watchlist.schema import parse_watchlist

DAY1 = date(2026, 10, 12)
DAY2 = date(2026, 10, 20)

NSE_HEADER = "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE"  # noqa: E501


def make_isin(body: str) -> str:
    """9-char body -> a 12-char ISIN with a correct Luhn check digit (independent of the code)."""
    stem = "IN" + body
    digits = "".join(str(int(c, 36)) for c in stem)
    total = 0
    for pos, ch in enumerate(reversed(digits)):  # check digit is appended, so start doubling at 1
        n = int(ch)
        if pos % 2 == 0:
            n *= 2
            n = n - 9 if n > 9 else n
        total += n
    return stem + str((10 - total % 10) % 10)


ALPHA = make_isin("E111A0101")
BETA = make_isin("E222B0102")
GAMMA1 = make_isin("E333C0103")
GAMMA2 = make_isin("E444C0104")
DELTA = make_isin("E555D0105")


def synthetic_records() -> list[SecurityRecord]:
    """Five fictional companies; two share a normalised name."""
    return [
        SecurityRecord(ALPHA, "Alpha Industries Limited", "ALPHA", "500001"),
        SecurityRecord(BETA, "Beta Motors Ltd.", "BETAMOT", "500002"),
        SecurityRecord(GAMMA1, "Gamma Finance Limited", "GAMMAF", None),
        SecurityRecord(GAMMA2, "Gamma Finance Ltd", "GAMMAF2", None),
        SecurityRecord(DELTA, "Delta Technologies India Limited", "DELTA", "500005"),
    ]


@pytest.fixture
def master() -> SecuritiesMaster:
    return SecuritiesMaster(synthetic_records())


@pytest.fixture
def run_sync(master: SecuritiesMaster):
    """Callable: (yaml-like dict, day, master) -> SyncReport against a fresh/initialised DB."""

    def _run(
        companies: list[dict],
        day: date = DAY1,
        use_master: SecuritiesMaster | None = None,
    ) -> SyncReport:
        init_db()
        return sync_watchlist(
            master=use_master or master,
            watchlist=parse_watchlist({"companies": companies}),
            today=day,
        )

    return _run


def rows(table: sa.Table, *where: sa.ColumnElement) -> list[sa.Row]:
    """All rows of a table matching the conditions."""
    with transaction() as conn:
        return list(conn.execute(sa.select(table).where(*where)))


def entity(isin: str) -> sa.Row:
    """The entity row for an ISIN."""
    (row,) = rows(tables.entities, tables.entities.c.isin == isin)
    return row


def write_file(path: Path, text: str) -> Path:
    """Write text (creating folders) and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def kit() -> SimpleNamespace:
    """Shared constants and helpers (tests cannot import from conftest in importlib mode)."""
    return SimpleNamespace(
        ALPHA=ALPHA, BETA=BETA, GAMMA1=GAMMA1, GAMMA2=GAMMA2, DELTA=DELTA,
        DAY1=DAY1, DAY2=DAY2, NSE_HEADER=NSE_HEADER, make_isin=make_isin,
        rows=rows, entity=entity, write_file=write_file, records=synthetic_records,
    )  # fmt: skip
