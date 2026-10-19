"""The gap-fill gate: a PDF/OCR value is current only if it is confident and its column adds up."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from finrag.core.config import ConfigError
from finrag.core.db import tables, transaction
from finrag.core.types import ValidationStatus
from finrag.facts.current_view import get_fact, get_facts, load_facts
from finrag.facts.gap_gate import (
    REASON_COLUMN_IDENTITY,
    REASON_INPUT_REJECTED,
    REASON_LOW_CONFIDENCE,
    REASON_OPENING_DATE,
    load_gate_identities,
    run_gate,
)

REAL = Path(__file__).resolve().parents[2]
PDF = "FIL-pdf000000001"
XBRL = "FIL-xbrl00000001"


@pytest.fixture(autouse=True)
def gate_registry(isolated_repo: Path) -> None:
    """The gate reads its identities from the real validation-rule registry."""
    shutil.copy(
        REAL / "config" / "validation_rules.yaml",
        isolated_repo / "config" / "validation_rules.yaml",
    )


@pytest.fixture
def gate(kit: SimpleNamespace) -> SimpleNamespace:
    """Helpers: add PDF facts with a statement and a period start, run the gate, read decisions."""
    kit.add_filing(PDF, fmt="pdf")
    kit.add_filing(XBRL)

    def pdf(concept, value, period="Q4", confidence=0.8, statement="profit_and_loss",
            start="set", extractor="ocr", filing=PDF):  # fmt: skip
        c = kit.cand(filing, period, value, concept, extractor, confidence)
        begin = kit.FY26[period][0].isoformat() if start == "set" else None
        with transaction() as conn:
            conn.execute(
                sa.update(tables.parsed_facts)
                .where(tables.parsed_facts.c.id == c.parsed_fact_id)
                .values(statement_type=statement, period_start=begin)
            )
        kit.write([c])
        return c

    def decisions():
        with transaction() as conn:
            return conn.execute(
                sa.select(tables.validation_results).order_by(tables.validation_results.c.id)
            ).all()

    def items():
        with transaction() as conn:
            return conn.execute(
                sa.select(tables.review_queue).where(
                    tables.review_queue.c.item_type == "gap_fill_rejected"
                )
            ).all()

    return SimpleNamespace(pdf=pdf, decisions=decisions, items=items, kit=kit)


def fact_now(kit, concept: str, period: str = "Q4"):
    from finrag.core.types import Period, PeriodType

    start, end, kind = kit.FY26[period]
    return get_fact(kit.ISIN, concept, Period(start, end, PeriodType(kind)), "consolidated")


def good_pl(gate, period="Q4", pat="304790000"):
    """A P&L column in rupees: revenue + other income = total income; PBT - tax = profit."""
    gate.pdf("revenue", "3012120000", period)
    gate.pdf("other_income", "88000000", period)
    gate.pdf("total_income", "3100120000", period)
    gate.pdf("profit_before_tax", "416060000", period)
    gate.pdf("tax_expense", "111270000", period)
    gate.pdf("profit_after_tax", pat, period)


def test_a_confident_column_that_adds_up_is_current(gate) -> None:
    good_pl(gate)
    report = run_gate(gate.kit.ISIN)
    assert report.rejected_facts == 0 and report.rows_written == 0 and gate.items() == []
    assert fact_now(gate.kit, "profit_after_tax").value == gate.kit.D("304790000")


def test_a_column_whose_profit_is_100x_off_is_rejected_whole(gate) -> None:
    """Real Netweb Q1 FY26 case: profit 30,479 (for 304.79) against PBT 416.06 - tax 111.27."""
    good_pl(gate, pat="30479000000")  # 30,479 million: printed without its decimal point
    gate.kit.write([])
    report = run_gate(gate.kit.ISIN)
    assert report.rejected_columns == 1 and report.rejected_facts == 6
    for concept in (
        "revenue",
        "total_income",
        "profit_before_tax",
        "tax_expense",
        "profit_after_tax",
    ):
        assert fact_now(gate.kit, concept) is None  # the good cells of a bad column are not used
    stored = {f.concept: f for f in load_facts_all(gate)}
    assert stored["profit_after_tax"].gap_rejected  # but nothing is deleted
    (row,) = gate.decisions()
    assert row.status == ValidationStatus.GAP_FILL_REJECTED.value and row.rule_id.startswith(
        "gap_fill_gate:"
    )
    details = json.loads(row.details_json)
    assert details["failed_identities"] == ["profit_after_tax"] and details["column_rejected"]
    assert {r for f in details["rejected_facts"] for r in f["reasons"]} == {REASON_COLUMN_IDENTITY}


def load_facts_all(gate):
    with transaction() as conn:
        return load_facts(conn, gate.kit.ISIN, include_rejected=True)


def test_a_rejected_column_raises_one_review_item_and_a_rerun_adds_nothing(gate) -> None:
    good_pl(gate, pat="30479000000")
    run_gate(gate.kit.ISIN)
    (item,) = gate.items()
    payload = json.loads(item.payload_json)
    assert payload["failed_identities"] == ["profit_after_tax"] and payload["filing_id"] == PDF
    again = run_gate(gate.kit.ISIN)
    assert again.rows_written == 0
    assert len(gate.items()) == 1 and len(gate.decisions()) == 1


def test_a_low_confidence_cell_is_refused_alone(gate) -> None:
    good_pl(gate)
    gate.pdf("employee_expense", "100000000", confidence=0.4)
    run_gate(gate.kit.ISIN)
    assert fact_now(gate.kit, "employee_expense") is None
    assert fact_now(gate.kit, "profit_after_tax") is not None  # the rest of the column stands
    (row,) = gate.decisions()
    (rejected,) = json.loads(row.details_json)["rejected_facts"]
    assert (
        rejected["reasons"] == [REASON_LOW_CONFIDENCE] and rejected["concept"] == "employee_expense"
    )


def test_the_minimum_confidence_itself_is_enough(gate) -> None:
    from finrag.core.config import get_settings

    gate.pdf(
        "employee_expense", "100000000", confidence=get_settings().parsing.pdf.min_table_confidence
    )
    assert run_gate(gate.kit.ISIN).rejected_facts == 0


def test_an_xbrl_fact_wins_anyway_so_a_rejection_there_changes_nothing(gate) -> None:
    gate.kit.write([gate.kit.cand(XBRL, "Q4", "3012120000", "revenue")])
    gate.pdf("revenue", "301212000000", confidence=0.3)  # 100x wrong, but shadowed by XBRL
    report = run_gate(gate.kit.ISIN)
    assert report.rejected_facts == 1 and report.review_items == 0 and gate.items() == []
    assert fact_now(gate.kit, "revenue").value == gate.kit.D("3012120000")


def test_a_balance_sheet_with_one_total_100x_off_is_rejected(gate) -> None:
    """The real Gabriel Q2 FY26 standalone case: total equity and liabilities printed 2010572."""
    gate.pdf("total_assets", "20105720000", statement="balance_sheet", confidence=0.71)
    gate.pdf(
        "total_equity_and_liabilities", "2010572000000", statement="balance_sheet", confidence=0.3
    )
    gate.pdf("cash_and_equivalents", "1088900000", statement="balance_sheet", confidence=0.71)
    run_gate(gate.kit.ISIN)
    for concept in ("total_assets", "total_equity_and_liabilities", "cash_and_equivalents"):
        assert fact_now(gate.kit, concept) is None
    reasons = {
        f["concept"]: f["reasons"]
        for f in json.loads(gate.decisions()[0].details_json)["rejected_facts"]
    }
    assert reasons["total_assets"] == [REASON_COLUMN_IDENTITY]
    assert set(reasons["total_equity_and_liabilities"]) == {
        REASON_LOW_CONFIDENCE,
        REASON_COLUMN_IDENTITY,
    }


def test_a_balanced_balance_sheet_passes(gate) -> None:
    gate.pdf("total_assets", "20105720000", statement="balance_sheet")
    gate.pdf("total_equity_and_liabilities", "20105720000", statement="balance_sheet")
    assert run_gate(gate.kit.ISIN).rejected_facts == 0


def test_an_identity_with_a_missing_input_is_not_evaluated(gate) -> None:
    gate.pdf("profit_after_tax", "304790000")  # no PBT or tax in the column: nothing to test
    assert run_gate(gate.kit.ISIN).rejected_facts == 0


def test_identities_are_judged_per_column_and_statement(gate) -> None:
    good_pl(gate, "Q3")  # a good column ...
    good_pl(gate, "Q4", pat="30479000000")  # ... next to a bad one of the same filing
    run_gate(gate.kit.ISIN)
    assert fact_now(gate.kit, "profit_after_tax", "Q3") is not None
    assert fact_now(gate.kit, "profit_after_tax", "Q4") is None


def test_an_opening_balance_of_a_column_without_a_start_is_refused(gate) -> None:
    gate.pdf(
        "opening_cash", "2395706800000", "Q4", statement="cash_flow", start="none", extractor="ocr"
    )
    gate.pdf(
        "closing_cash", "1651422700000", "Q4", statement="cash_flow", start="none", extractor="ocr"
    )
    run_gate(gate.kit.ISIN)
    assert fact_now(gate.kit, "opening_cash") is None
    assert (
        fact_now(gate.kit, "closing_cash") is not None
    )  # a closing balance sits on the column end
    (row,) = gate.decisions()
    (rejected,) = json.loads(row.details_json)["rejected_facts"]
    assert rejected["reasons"] == [REASON_OPENING_DATE]


def test_a_derived_fact_built_on_a_rejected_input_is_not_current(gate) -> None:
    c = gate.pdf("revenue", "301212000000", "Q4", confidence=0.3)
    derived_id = "FCT-DERIVED000000000000000001"
    with transaction() as conn:
        conn.execute(
            sa.insert(tables.canonical_facts).values(
                fact_id=derived_id,
                isin=gate.kit.ISIN,
                concept="revenue",
                period_start="2025-10-01",
                period_end="2026-03-31",
                period_type="H",
                basis="consolidated",
                value="1",
                unit="INR",
                source_filing_id=PDF,
                parsed_fact_id=None,
                mapping_method=None,
                is_derived=1,
                derivation_json=json.dumps({"input_fact_ids": [_fact_id(c)]}),
                filed_on=None,
                created_at="2026-05-02T00:00:00",
                source_class="derived",
            )  # fmt: skip
        )
    run_gate(gate.kit.ISIN)
    with transaction() as conn:
        found = {f.fact_id: f for f in load_facts(conn, gate.kit.ISIN, include_rejected=True)}
    assert found[derived_id].gap_rejected and found[derived_id].gap_reason == REASON_INPUT_REJECTED
    assert derived_id not in {f.fact_id for f in get_facts(gate.kit.ISIN, ["revenue"])}


def _fact_id(candidate) -> str:
    with transaction() as conn:
        return conn.execute(
            sa.select(tables.canonical_facts.c.fact_id).where(
                tables.canonical_facts.c.parsed_fact_id == candidate.parsed_fact_id
            )
        ).scalar_one()


def test_writing_the_same_candidates_again_does_not_duplicate_a_rejected_fact(gate) -> None:
    c = gate.pdf("revenue", "301212000000", "Q4", confidence=0.3)
    run_gate(gate.kit.ISIN)
    stats = gate.kit.write([c])
    assert stats.inserted == 0 and stats.skipped == 1


def test_a_later_pass_decision_makes_the_facts_current_again(gate) -> None:
    gate.pdf("revenue", "3012120000", "Q4", confidence=0.4)
    run_gate(gate.kit.ISIN)
    assert fact_now(gate.kit, "revenue") is None
    (old,) = gate.decisions()
    with transaction() as conn:  # a newer decision for the same column supersedes the older one
        conn.execute(
            sa.insert(tables.validation_results).values(
                isin=old.isin,
                period_end=old.period_end,
                period_type=old.period_type,
                basis=old.basis,
                rule_id=old.rule_id,
                status=ValidationStatus.PASS.value,
                details_json=json.dumps({"rejected_facts": []}),
                run_id="RUN-x",
                created_at="z",
            )  # fmt: skip
        )
    assert fact_now(gate.kit, "revenue") is not None


def test_dry_run_writes_nothing(gate) -> None:
    good_pl(gate, pat="30479000000")
    report = run_gate(gate.kit.ISIN, dry_run=True)
    assert report.rejected_columns == 1 and gate.decisions() == [] and gate.items() == []


def test_the_gate_names_only_known_identities(isolated_repo: Path) -> None:
    path = isolated_repo / "config" / "validation_rules.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[total_income, profit_after_tax, balance_sheet]", "[nope]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown identity 'nope'"):
        load_gate_identities()


def test_the_real_registry_names_the_three_identities() -> None:
    assert [i.id for i in load_gate_identities()] == [
        "total_income",
        "profit_after_tax",
        "balance_sheet",
    ]
