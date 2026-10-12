"""A revision's filed_on comes from the exchange filename timestamp when its board date matches."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import sqlalchemy as sa

from finrag.core.db import init_db, tables, transaction
from finrag.core.types import Basis, FilingFormat, PeriodType
from finrag.ingestion.adapters.local_inbox import _REVISION_RE
from finrag.ingestion.models import FilingCandidate
from finrag.ingestion.runner import revision_filed_on
from finrag.ingestion.xbrl_meta import XbrlMeta

ISIN = "INE111A01010"
BOARD = date(2026, 5, 16)


def _setup(original_filed_on: str | None) -> None:
    init_db()
    with transaction() as conn:
        conn.execute(sa.insert(tables.entities).values(isin=ISIN, name="X", status="active"))
        if original_filed_on is not None:
            conn.execute(
                sa.insert(tables.filings).values(
                    filing_id="FIL-o",
                    isin=ISIN,
                    filing_type="financial_results",
                    submission="original",
                    revision_no=0,
                    basis="standalone",
                    period_end="2026-03-31",
                    filed_on=original_filed_on,
                    format="xbrl",
                    status="parsed",
                    sha256="o",
                    created_at="x",
                )  # fmt: skip
            )


def _candidate(name: str, submission: str = "revision") -> FilingCandidate:
    return FilingCandidate(
        identifier=ISIN, source="local_inbox", filing_type="financial_results",
        format=FilingFormat.XBRL, isin=ISIN, submission=submission, basis=Basis.STANDALONE,
        period_start=date(2026, 1, 1), period_end=date(2026, 3, 31), period_type=PeriodType.Q,
        local_path=Path(name),
    )  # fmt: skip


NAME = "INTEGRATED_FILING_INDAS_1668217_16052026103806_WEB.xml"


def test_timestamp_used_when_board_date_equals_the_originals() -> None:
    _setup("2026-05-16")
    got = revision_filed_on(_candidate(NAME), XbrlMeta(board_meeting_date=BOARD))
    assert got == "2026-05-16T10:38:06"


def test_not_used_when_board_date_differs_or_original_unknown_or_not_a_revision() -> None:
    _setup("2026-05-02")
    assert revision_filed_on(_candidate(NAME), XbrlMeta(board_meeting_date=BOARD)) is None
    assert (
        revision_filed_on(_candidate(NAME, "original"), XbrlMeta(board_meeting_date=BOARD)) is None
    )


def test_original_missing_gives_none(isolated_repo) -> None:
    _setup(None)
    assert revision_filed_on(_candidate(NAME), XbrlMeta(board_meeting_date=BOARD)) is None


def test_convention_name_may_carry_the_timestamp() -> None:
    assert _REVISION_RE.match("rev1")
    assert _REVISION_RE.match("rev1_16052026103806_")
    assert not _REVISION_RE.match("rev1_garbage")
