"""Checks against the real canonical_facts in data/finrag.db (skipped until it is populated).

The real database is opened read-only; `evaluate_isin` reads and returns, it writes nothing.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from finrag.core.db import tables
from finrag.core.types import ValidationStatus
from finrag.facts.current_view import get_facts
from finrag.facts.gap_gate import evaluate as gate_evaluate
from finrag.validation.cross_source import OCR_ERROR, RESTATED
from finrag.validation.engine import evaluate_isin
from finrag.validation.rules import load_rules

REAL = Path(__file__).resolve().parents[2]
DB = REAL / "data" / "finrag.db"
NETWEB, GABRIEL, HDFC = "INE0NT901020", "INE524A01029", "INE040A01034"
NETWEB_PDF = "FIL-84bf2a7b31af"  # Q4 FY26 results (scan)
NETWEB_Q2_PDF = "FIL-adc5c2c447ce"  # Q2 FY26 results (scan)
NETWEB_Q1_FY27_PDF = "FIL-fe39ea35d094"
GABRIEL_Q2_PDF = "FIL-18d88e93949e"
HDFC_Q2_PDF, HDFC_Q4_PDF = "FIL-13f40167ba80", "FIL-2d754667c7d5"
HDFC_Q4_FY25_PDF = "FIL-771ea082ef81"
MN = Decimal(1_000_000)


def _engine() -> sa.engine.Engine | None:
    """Read-only engine on the real database, or None when it has no canonical facts."""
    if not DB.exists():
        return None
    engine = sa.create_engine(f"sqlite:///file:{DB.as_posix()}?mode=ro&uri=true")
    try:
        with engine.connect() as conn:
            found = conn.execute(sa.select(sa.func.count()).select_from(tables.canonical_facts))
            if not found.scalar():
                return None
    except sa.exc.OperationalError:
        return None
    return engine


ENGINE = _engine()
pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(ENGINE is None, reason="real canonical_facts not populated"),
]


@pytest.fixture(scope="module")
def evaluations():
    """ISIN -> Evaluation of the real data (computed once)."""
    return {
        isin: evaluate_isin(isin, load_rules(), engine=ENGINE) for isin in (NETWEB, GABRIEL, HDFC)
    }


def statuses(evaluation, rule_id: str) -> Counter:
    """Status -> number of results of a rule."""
    return Counter(r.status for r in evaluation.results if r.rule_id == rule_id)


@pytest.fixture(scope="module")
def gate():
    """isin -> {(filing, statement, basis, start, end): Column} the gap-fill gate decides."""
    return {
        isin: {
            (c.filing_id, c.statement_type, c.basis, c.period_start, c.period_end): c
            for c in gate_evaluate(isin, engine=ENGINE)
        }
        for isin in (NETWEB, GABRIEL, HDFC)
    }


def would_fill_rejections(gate) -> set[tuple[str, ...]]:
    """(isin, filing, statement, basis, start, end) of columns whose rejection changes the view."""
    return {
        (isin, *key)
        for isin, columns in gate.items()
        for key, column in columns.items()
        if column.changes_current_view
    }


def test_every_balance_sheet_balances_banks_included(evaluations) -> None:
    # Gabriel: six XBRL balance sheets (three dates, both bases; the standalone ones came with the
    # inbox of 2026-10-20, before that the standalone comparatives were PDF text).
    gabriel = {
        (r.period_end, r.basis): r
        for r in evaluations[GABRIEL].results
        if r.rule_id == "bs_balances"
    }
    assert {k: r.status for k, r in gabriel.items()} == dict.fromkeys(
        [
            ("2025-03-31", "consolidated"),
            ("2025-09-30", "consolidated"),
            ("2026-03-31", "consolidated"),
            ("2025-03-31", "standalone"),
            ("2025-09-30", "standalone"),
            ("2026-03-31", "standalone"),
        ],
        ValidationStatus.PASS,
    )
    assert all(r.details["sources"] == {"left": "xbrl", "right": "xbrl"} for r in gabriel.values())
    assert statuses(evaluations[NETWEB], "bs_balances") == {ValidationStatus.PASS: 3}
    assert statuses(evaluations[HDFC], "bank_bs_balances") == {
        ValidationStatus.PASS: 8
    }  # 4 dates (to Sep 2026) x 2 bases


def test_gabriel_standalone_h1_fy26_pdf_balance_sheet_is_no_longer_needed_and_never_current(
    gate,
) -> None:
    """Q2 FY26 PDF, standalone, 30 Sep 2025: total equity and liabilities is printed 2010572.

    Total assets is 20,105.72 mn; the right side lost its decimal point (a 100x error in the PDF
    text layer). Until the standalone Q2 FY26 XBRL was added the PDF totals would have been
    current and the balance-sheet rule would have blocked, so the gate rejected the column. The
    XBRL now supplies the balance sheet: the column no longer changes the current view, and the
    current facts are the filed XBRL ones (which balance).
    """
    changing = [
        key
        for key, column in gate[GABRIEL].items()
        if column.changes_current_view and key[0] == GABRIEL_Q2_PDF and key[1] == "balance_sheet"
    ]
    assert changing == []
    current = {
        f.concept: f
        for f in get_facts(GABRIEL, ["total_assets", "total_equity_and_liabilities"], None,
                           "standalone", engine=ENGINE)
        if str(f.period.end) == "2025-09-30"
    }  # fmt: skip
    assert set(current) == {"total_assets", "total_equity_and_liabilities"}
    assert all(f.source_class.value == "xbrl" for f in current.values())
    assert current["total_assets"].value == current["total_equity_and_liabilities"].value


def test_opening_cash_equals_prior_closing_on_real_data(evaluations) -> None:
    rule = "opening_cash_equals_prior_closing"
    assert statuses(evaluations[GABRIEL], rule) == {
        ValidationStatus.PASS: 2,  # both bases
        ValidationStatus.SKIP: 2,
    }
    assert statuses(evaluations[NETWEB], rule) == {
        ValidationStatus.PASS: 1,
        ValidationStatus.SKIP: 1,
    }
    # HDFC: FY25 passes on both bases, FY26 consolidated passes; FY26 standalone is the one real
    # block (see test_the_only_block_is_hdfc_standalone_opening_cash_of_h1_fy27)
    assert statuses(evaluations[HDFC], rule) == {
        ValidationStatus.PASS: 3,
        ValidationStatus.SKIP: 2,
        ValidationStatus.BLOCK: 1,
    }
    for evaluation in evaluations.values():
        for row in evaluation.results:
            if row.rule_id == rule and row.status is ValidationStatus.SKIP:
                assert row.period_end == "2024-03-31"  # FY25's opening: there is no FY24 closing
                assert row.details["missing"] == ["closing_cash"]


def test_hdfc_pdf_opening_cash_is_never_current_where_its_date_is_unverifiable(gate) -> None:
    """HDFC cash-flow headers carry dates only, so the PDF columns have no period start.

    The opening balance was dated at the column end (2,395,706.8 mn "at 30 Sep 2025" against an
    XBRL closing cash of 1,651,422.7 mn), which blocked the standalone cash lines. That was a
    date-mapping fault, not an OCR error (M04 now refuses such a balance); the same columns also
    carry real OCR errors (closing cash 4,651,422.8 for 1,651,422.7; 25633937 without decimals).
    The Q4 FY26 column no longer needs refusing: the H1 FY27 XBRL gives the opening cash at
    31 Mar 2026 itself (and disagrees with the FY26 closing cash, see the block test).
    """
    refused = {
        key: sorted({r for f in column.rejected for r in f.reasons})
        for key, column in gate[HDFC].items()
        if column.statement_type == "cash_flow" and column.changes_current_view
    }
    both = ["low_confidence", "opening_date_unverifiable"]
    assert refused == {
        (HDFC_Q2_PDF, "cash_flow", "standalone", "2024-09-30", "2024-09-30"): both,
        (HDFC_Q2_PDF, "cash_flow", "standalone", "2025-09-30", "2025-09-30"): both,
    }
    current = get_facts(HDFC, ["opening_cash"], None, "standalone", engine=ENGINE)
    assert current and {f.source_class.value for f in current} == {"xbrl"}


def test_income_sums_hold_on_real_data(evaluations) -> None:
    for isin, rule in (
        (GABRIEL, "pl_total_income"),
        (NETWEB, "pl_total_income"),
        (HDFC, "bank_pl_total_income"),
    ):
        found = statuses(evaluations[isin], rule)
        assert set(found) <= {ValidationStatus.PASS, ValidationStatus.SKIP}, (isin, found)
        assert found[ValidationStatus.PASS] >= 9
    # A skip is a period whose PDF comparative column carries no total income line (the Q2 FY25 and
    # H1 FY25 columns of the Q2 FY26 results PDF, and H2 FY25 derived from them): nothing is wrong.
    skipped = {
        (r.period_type, r.period_end): r.details["missing"]
        for r in evaluations[NETWEB].results
        if r.rule_id == "pl_total_income" and r.status is ValidationStatus.SKIP
    }
    assert skipped == dict.fromkeys(
        [("Q", "2024-09-30"), ("H", "2024-09-30"), ("H", "2025-03-31")], ["total_income"]
    )
    assert statuses(evaluations[GABRIEL], "pl_total_income") == {
        ValidationStatus.PASS: 22,  # both bases since the inbox of 2026-10-20
        ValidationStatus.SKIP: 1,  # standalone FY24: a PDF comparative with no income lines
    }


def test_the_only_block_is_hdfc_standalone_opening_cash_of_h1_fy27(evaluations) -> None:
    """A real inconsistency between two XBRL filings, found when the H1 FY27 filing was added.

    HDFC Bank standalone closing cash at 31 Mar 2026 is 2,976,068.4 mn in the FY26 results; the
    H1 FY27 results open the half year at 2,984,663.6 mn (8,595.2 mn, 0.29 % more). Both are
    filed XBRL values, so nothing is wrong with the extraction: the later filing restated its
    opening balance (or one of the two is mistaken). The cash lines of that date stay blocked
    until a person looks at it; every other rule passes or warns.
    """
    blocked = [
        (r.isin, r.period_end, r.basis, r.rule_id)
        for evaluation in evaluations.values()
        for r in evaluation.results
        if r.status is ValidationStatus.BLOCK
    ]
    assert blocked == [(HDFC, "2026-03-31", "standalone", "opening_cash_equals_prior_closing")]
    row = next(r for r in evaluations[HDFC].results if r.status is ValidationStatus.BLOCK)
    assert row.details["sources"] == {"left": "xbrl", "right": "xbrl"}
    assert (row.details["left"], row.details["right"]) == ("2984663600000", "2976068400000")


def test_the_columns_that_would_have_changed_the_view_are_rejected_for_cause(gate) -> None:
    """Every PDF/OCR column that would fill a gap in the current view is listed; each is rejected.

    Since the inbox of 2026-10-20 the Netweb Q1 FY26, Gabriel standalone Q2 FY26 and HDFC Q4 FY26
    cash-flow columns have XBRL to cover them and are no longer on this list. The list below is
    PDF-only values below parsing.pdf.min_table_confidence (older comparatives, most of them
    FY24) and the two HDFC cash-flow columns whose date is unverifiable. Two columns are parse
    leftovers: Gabriel Q2 FY26 standalone cash flow read as 2020-04-01..2020-09-30, and an
    'unknown' statement page read as 2024-10-01..2025-03-31; both stay rejected.
    """
    pl = "profit_and_loss"
    assert would_fill_rejections(gate) == {
        (HDFC, HDFC_Q2_PDF, "cash_flow", "standalone", "2024-09-30", "2024-09-30"),
        (HDFC, HDFC_Q2_PDF, "cash_flow", "standalone", "2025-09-30", "2025-09-30"),
        (HDFC, HDFC_Q4_FY25_PDF, pl, "consolidated", "2023-04-01", "2024-03-31"),
        (HDFC, HDFC_Q4_FY25_PDF, pl, "consolidated", "2024-01-01", "2024-03-31"),
        (HDFC, HDFC_Q4_FY25_PDF, pl, "consolidated", "2024-10-01", "2024-12-31"),
        (HDFC, HDFC_Q4_FY25_PDF, pl, "standalone", "2023-04-01", "2024-03-31"),
        (HDFC, HDFC_Q4_FY25_PDF, pl, "standalone", "2024-01-01", "2024-03-31"),
        (HDFC, HDFC_Q4_FY25_PDF, pl, "standalone", "2024-10-01", "2024-12-31"),
        (GABRIEL, "FIL-133dad08692c", "balance_sheet", "standalone", "2024-03-31", "2024-03-31"),
        (GABRIEL, "FIL-133dad08692c", pl, "standalone", "2023-04-01", "2024-03-31"),
        (GABRIEL, "FIL-133dad08692c", pl, "standalone", "2024-01-01", "2024-03-31"),
        (GABRIEL, GABRIEL_Q2_PDF, "cash_flow", "standalone", "2020-04-01", "2020-09-30"),
        (GABRIEL, GABRIEL_Q2_PDF, pl, "consolidated", "2024-04-01", "2024-09-30"),
        (GABRIEL, GABRIEL_Q2_PDF, pl, "consolidated", "2024-07-01", "2024-09-30"),
        (GABRIEL, GABRIEL_Q2_PDF, "unknown", "consolidated", "2024-10-01", "2025-03-31"),
    }


def test_gabriel_cash_flow_differs_from_balance_sheet_cash(evaluations) -> None:
    rows = {
        (r.period_end, r.basis): r
        for r in evaluations[GABRIEL].results
        if r.rule_id == "cash_ties_to_bs"
    }
    # consolidated: the cash flow statement's cash differs from the balance-sheet line (bank
    # balances and the like); standalone, added with the inbox of 2026-10-20, ties exactly
    consolidated = {k[0]: v for k, v in rows.items() if k[1] == "consolidated"}
    assert {k: v.status for k, v in consolidated.items()} == dict.fromkeys(
        consolidated, ValidationStatus.WARN
    )
    gaps = {k: Decimal(v.details["abs_diff"]) / MN for k, v in consolidated.items()}
    expected = {"2025-03-31": "38.65", "2025-09-30": "34.41", "2026-03-31": "15.08"}
    assert gaps == {k: Decimal(v) for k, v in expected.items()}
    standalone = {k[0]: v for k, v in rows.items() if k[1] == "standalone"}
    assert {k: (v.status, v.details["abs_diff"]) for k, v in standalone.items()} == dict.fromkeys(
        ["2025-03-31", "2025-09-30", "2026-03-31"], (ValidationStatus.PASS, "0")
    )
    # Netweb ties exactly at its three XBRL dates; the fourth result is a skip: the H1 FY25 closing
    # cash of the Q2 FY26 PDF (30 Sep 2024) has no balance-sheet cash to compare with.
    netweb = {
        r.period_end: r for r in evaluations[NETWEB].results if r.rule_id == "cash_ties_to_bs"
    }
    assert {k: v.status for k, v in netweb.items()} == {
        "2024-09-30": ValidationStatus.SKIP,
        "2025-03-31": ValidationStatus.PASS,
        "2025-09-30": ValidationStatus.PASS,
        "2026-03-31": ValidationStatus.PASS,
    }
    assert netweb["2024-09-30"].details["missing"] == ["cash_and_equivalents"]


def netweb_rows(evaluations):
    return [r for r in evaluations[NETWEB].results if r.rule_id == "xbrl_vs_pdf"]


def test_netweb_cross_source_findings(evaluations) -> None:
    rows = netweb_rows(evaluations)
    warned = {(r.period_end, r.period_type): r for r in rows if r.status is ValidationStatus.WARN}
    # the third is Q1 FY26: the scan's profit 30,479 against the XBRL's 304.79 (100x)
    assert set(warned) == {("2025-03-31", "INSTANT"), ("2025-06-30", "Q"), ("2026-03-31", "Q")}
    assert warned[("2025-06-30", "Q")].details["concepts"] == ["profit_after_tax"]
    assert set(warned[("2025-03-31", "INSTANT")].details["concepts"]) == {
        "total_assets", "total_equity_and_liabilities", "total_equity", "other_equity",
        "total_current_assets",
    }  # fmt: skip
    # two OCR errors, one in each PDF that carries the Q4 FY26 column (see the OCR-error test)
    assert warned[("2026-03-31", "Q")].details["concepts"] == ["finance_cost", "profit_before_tax"]


def test_netweb_fy25_is_a_restated_comparative(evaluations) -> None:
    """The inventory-valuation change (corporate event 1) shows in two PDFs: the Q4 FY26 results
    restate FY25 (P&L, Q4 FY25 and balance sheet, 21 lines) and the Q2 FY26 results already
    restate the FY25 year column (5 lines). Both are genuine restated comparatives."""
    clusters = [
        (r.period_type, c)
        for r in netweb_rows(evaluations)
        for c in r.details.get("clusters", [])
        if c["classification"] == RESTATED
    ]
    by_filing: dict[str, list] = {}
    for kind, c in clusters:
        by_filing.setdefault(c["pdf_filing"], []).append((kind, c))
    assert set(by_filing) == {NETWEB_PDF, NETWEB_Q2_PDF}
    found = [
        c for _, c in by_filing[NETWEB_PDF]
    ]  # the one cluster is shown on every row it touches
    assert len(found) == 3 and all(c == found[0] for c in found)
    ((q2_kind, q2),) = by_filing[NETWEB_Q2_PDF]
    assert (q2_kind, q2["period_end"], q2["differing_keys"], q2["periods_affected"]) == (
        "FY", "2025-03-31", 5, ["FY"],
    )  # fmt: skip
    cluster = found[0]
    assert (cluster["pdf_filing"], cluster["period_end"], cluster["differing_keys"]) == (
        NETWEB_PDF, "2025-03-31", 21,
    )  # fmt: skip
    assert cluster["periods_affected"] == ["FY", "INSTANT", "Q"]
    holds = {(e["identity"], e["period_end"]): e["pdf_holds"] for e in cluster["identities"]}
    assert holds and all(holds.values())  # every identity that includes a restated line holds
    assert {"profit_before_tax", "profit_after_tax", "total_expenses", "total_equity"} <= {
        i for i, _ in holds
    }
    fy = next(
        r for r in netweb_rows(evaluations) if (r.period_end, r.period_type) == ("2025-03-31", "FY")
    )
    assert fy.status is ValidationStatus.PASS  # 9.68 mn is under the absolute tolerance
    pbt = next(d for d in fy.details["differences"] if d["concept"] == "profit_before_tax")
    assert (Decimal(pbt["xbrl"]) / MN, Decimal(pbt["pdf"]) / MN) == (
        Decimal("1539.68"),
        Decimal("1530.00"),
    )
    pat = next(d for d in fy.details["differences"] if d["concept"] == "profit_after_tax")
    assert (Decimal(pat["xbrl"]) / MN, Decimal(pat["pdf"]) / MN) == (
        Decimal("1144.75"),
        Decimal("1137.51"),
    )
    assert {(c.pdf_filing_id, str(c.period_end)) for _, c, _ in evaluations[NETWEB].restated} == {
        (NETWEB_PDF, "2025-03-31"),
        (NETWEB_Q2_PDF, "2025-03-31"),
    }


def test_netweb_q4_fy26_has_two_ocr_errors_in_two_pdfs(evaluations) -> None:
    """Q4 FY26 (quarter to 31 Mar 2026) read twice: in its own results scan, profit before tax
    94,822 for 948.22; in the Q1 FY27 results (comparative column), finance cost 8,153 for 81.53
    (both 100x, confidence 0.3). XBRL has both right; neither OCR value is current."""
    row = next(
        r for r in netweb_rows(evaluations) if (r.period_end, r.period_type) == ("2026-03-31", "Q")
    )
    clusters = {c["pdf_filing"]: c for c in row.details["clusters"]}
    assert set(clusters) == {NETWEB_PDF, NETWEB_Q1_FY27_PDF}
    assert all(
        c["classification"] == OCR_ERROR and c["differing_keys"] == 1 for c in clusters.values()
    )
    diffs = {d["concept"]: d for d in row.details["differences"] if d["beyond_tolerance"]}
    assert set(diffs) == {"profit_before_tax", "finance_cost"}
    pbt, fin = diffs["profit_before_tax"], diffs["finance_cost"]
    assert (Decimal(pbt["xbrl"]), Decimal(pbt["pdf"])) == (
        Decimal(948_220_000),
        Decimal(94_822_000_000),
    )
    assert (Decimal(fin["xbrl"]), Decimal(fin["pdf"])) == (
        Decimal(81_530_000),
        Decimal(8_153_000_000),
    )
    for diff in diffs.values():
        assert set(diff["signals"]) == {"decimal_shift", "low_confidence"}
    current = {
        f.concept: f
        for f in get_facts(
            NETWEB, ["profit_before_tax", "finance_cost"], None, "standalone", engine=ENGINE
        )
        if str(f.period.end) == "2026-03-31" and f.period.type.value == "Q"
    }
    assert {c: f.source_class.value for c, f in current.items()} == dict.fromkeys(diffs, "xbrl")


# --- the Scheme of Arrangement: restated comparatives and the event that explains them ----------

GABRIEL_Q1_FY27_PDF = "FIL-83a2f50f0a6d"


def test_gabriel_restated_comparatives_are_explained_by_the_filings_own_note(evaluations) -> None:
    """Every comparative column of the Q1 FY27 results differs from the first filing, and says why.

    Standalone, the sums still hold (profit before tax less tax is profit) and the note explains it
    too; consolidated, the restated profit before tax now includes the associates' share, so the
    sums of the old layout do not hold, and the note is what explains it.
    """
    found = {
        (c.basis, c.period_end.isoformat()): c
        for isin, c, _ in evaluations[GABRIEL].restated
        if c.pdf_filing_id == GABRIEL_Q1_FY27_PDF
    }
    assert sorted(found) == [
        ("consolidated", "2025-06-30"),
        ("consolidated", "2026-03-31"),
        ("standalone", "2025-06-30"),
        ("standalone", "2026-03-31"),
    ]
    assert all(c.comparative for c in found.values())
    assert found[("standalone", "2025-06-30")].explained_by == ["identities", "note"]
    assert found[("consolidated", "2025-06-30")].explained_by == ["note"]
    pages = {k: c.note["page"] for k, c in found.items()}
    assert pages[("standalone", "2025-06-30")] == 4 and pages[("consolidated", "2025-06-30")] == 7
    assert (
        "figures for the previous periods have been restated accordingly"
        in (found[("consolidated", "2025-06-30")].note["text"])
    )


def test_gabriel_comparatives_with_nothing_to_explain_them_are_left_to_the_review_queue(
    evaluations,
) -> None:
    left = sorted(
        (c.basis, c.pdf_filing_id, c.period_end.isoformat(), c.classification)
        for _, c, _ in evaluations[GABRIEL].disagreements
    )
    assert left == [
        ("consolidated", "FIL-6173f7a73666", "2025-03-31", "unclear"),
        ("standalone", GABRIEL_Q2_PDF, "2025-03-31", "ocr_error_likely"),
    ]


def test_the_scheme_is_one_recorded_event_covering_both_bases() -> None:
    events = tables.corporate_events
    queue = tables.review_queue
    with ENGINE.connect() as conn:
        rows = list(conn.execute(sa.select(events).where(events.c.isin == GABRIEL)))
        proposals = {
            r.id: r.status
            for r in conn.execute(
                sa.select(queue).where(queue.c.item_type == "corporate_event_proposal")
            )
            if f'"filing_id": "{GABRIEL_Q1_FY27_PDF}"' in r.payload_json
        }
    (event,) = rows
    assert (event.event_type, event.event_date) == ("comparative_restatement", "2026-04-01")
    details = json.loads(event.details)
    assert details["bases"] == ["standalone", "consolidated"]
    assert details["pdf_filing_id"] == GABRIEL_Q1_FY27_PDF
    assert (
        "Scheme of Arrangement" in details["citation"]
        and "pooling of interest" in details["citation"]
    )
    # item 763 (ignored, the wrong reading of page 4's heading) and the four of this restatement
    assert Counter(proposals.values()) == {"resolved": 4, "ignored": 1}
