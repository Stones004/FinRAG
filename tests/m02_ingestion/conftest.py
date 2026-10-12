"""Fixtures for M02: synthetic entities, XBRL instances, PDFs and an inbox in a temp repo."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
import sqlalchemy as sa

from finrag.core import config
from finrag.core.db import init_db, tables, transaction

REAL_SECTIONS = Path(__file__).resolve().parents[2] / "config" / "sections.yaml"


def make_isin(body: str) -> str:
    """9-char body -> a 12-char ISIN with a correct Luhn check digit (independent of the code)."""
    stem = "IN" + body
    digits = "".join(str(int(c, 36)) for c in stem)
    total = 0
    for pos, ch in enumerate(reversed(digits)):
        n = int(ch)
        if pos % 2 == 0:
            n *= 2
            n = n - 9 if n > 9 else n
        total += n
    return stem + str((10 - total % 10) % 10)


ALPHA = make_isin("E111A0101")
BETA = make_isin("E222B0102")
ALPHA_TYPO = ALPHA.replace("E111", "EOOO")  # letters O: fails the check digit


@pytest.fixture(autouse=True)
def sections_yaml(isolated_repo: Path) -> None:
    """The temp repo gets the real sections.yaml (the global fixture copies only settings)."""
    shutil.copy(REAL_SECTIONS, isolated_repo / "config" / "sections.yaml")


@pytest.fixture
def db() -> None:
    """An initialised database with two active entities: ALPHA (ALPHAX) and BETA (BETAX)."""
    init_db()
    with transaction() as conn:
        for isin, name, symbol, code in (
            (ALPHA, "Alpha Industries Limited", "ALPHAX", "500001"),
            (BETA, "Beta Motors Limited", "BETAX", "500002"),
        ):
            conn.execute(
                sa.insert(tables.entities).values(
                    isin=isin,
                    name=name,
                    nse_symbol=symbol,
                    bse_code=code,
                    sector_type="general",
                    default_basis="consolidated",
                    status="active",
                    added_on="2026-10-12",
                    updated_on="2026-10-12",
                )
            )


def make_xbrl(
    isin: str = ALPHA,
    start: str = "2026-01-01",
    end: str = "2026-03-31",
    period_type: str = "Quarterly",
    basis: str = "Consolidated",
    board_date: str | None = "2026-05-02",
    family: str = "IndAS",
    symbol: str = "ALPHAX",
    note: str = "",
) -> bytes:
    """Build a minimal XBRL instance carrying the filing-level metadata elements."""
    cap = "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"
    ent = (
        f"http://www.sebi.gov.in/xbrl/IntegratedFinance_{family}/2026-01-31/in-capmkt/in-capmkt-ent"
    )

    def el(name: str, value: str, ctx: str = "OneD") -> str:
        return f'<in-capmkt:{name} contextRef="{ctx}">{value}</in-capmkt:{name}>'

    facts = [
        el("ScripCode", "500001"),
        el("Symbol", symbol),
        el("ISIN", isin),
        el("NameOfTheCompany", "ALPHA INDUSTRIES LIMITED"),
        el("DateOfBoardMeetingWhenFinancialResultsWereApproved", board_date) if board_date else "",
        el("LevelOfRounding", "Millions"),
        el("TypeOfReportingPeriod", period_type),
        el("ReportingQuarter", "Fourth quarter"),
        el("DateOfStartOfReportingPeriod", start),
        el("DateOfEndOfReportingPeriod", end),
        el("WhetherResultsAreAuditedOrUnaudited", "Audited"),
        el("NatureOfReportStandaloneConsolidated", basis),
        el("DateOfStartOfReportingPeriod", "2025-04-01", "FourD"),  # cumulative context follows
    ]
    header = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in-capmkt="{cap}"\n'
        f'  xmlns:in-capmkt-ent="{ent}">\n'
    )
    text = header + "\n".join(facts) + f"\n<!-- {note} -->\n</xbrli:xbrl>"
    return text.encode("utf-8")


def make_pdf(pages: list[str], uris: tuple[str, ...] = ()) -> bytes:
    """Build a PDF with one text page per string (and optional link annotations on page 1)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 60), text, fontsize=9)
    for uri in uris:
        doc[0].insert_link(
            {"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 50, 200, 70), "uri": uri}
        )
    data = doc.tobytes()
    doc.close()
    return data


COVER = "Dear Sir/Madam,\nSub: Transcript of Earnings Call for the quarter ended June 30, 2026\n"
TITLE = "Alpha Industries Q1 FY27 Earnings Conference Call\n"
QA = "Moderator: The next question is from the line of Jane Doe from ACME Securities.\n"


def full_transcript(quarter: str = "Q1 FY27") -> bytes:
    """A 4-page transcript: 1-page cover letter, title block, prepared remarks, Q&A."""
    return make_pdf(
        [
            COVER,
            f"Alpha Industries {quarter} Earnings Conference Call\nManagement: Welcome everyone.",
            "Management: Revenue grew strongly this quarter.",
            QA + "Jane Doe: How did margins move?",
        ]
    )


def link_only_letter(quarter: str = "Q1 FY26") -> bytes:
    """A one-page cover letter that only points to the company website."""
    return make_pdf(
        [
            f"Sub: Transcript of {quarter} Earnings Conference Call\n"
            "The transcript is available on our website at\n"
            "https://www.alpha.example/investors/transcripts\n"
        ],
        uris=("https://www.alpha.example/investors",),
    )


@pytest.fixture
def inbox(isolated_repo: Path) -> Path:
    """The (created) inbox folder of the temp repo."""
    folder = isolated_repo / "data" / "inbox"
    folder.mkdir(parents=True)
    return folder


def rows(table: sa.Table, *where: sa.ColumnElement) -> list[sa.Row]:
    """All rows of a table matching the conditions."""
    with transaction() as conn:
        return list(conn.execute(sa.select(table).where(*where)))


@pytest.fixture
def kit() -> SimpleNamespace:
    """Constants and builders (tests cannot import from conftest in importlib mode)."""
    return SimpleNamespace(
        ALPHA=ALPHA, BETA=BETA, ALPHA_TYPO=ALPHA_TYPO, make_isin=make_isin, make_xbrl=make_xbrl,
        make_pdf=make_pdf, full_transcript=full_transcript, link_only_letter=link_only_letter,
        rows=rows, DAY=date(2026, 10, 12), settings=config.get_settings,
    )  # fmt: skip
