"""PDF/OCR columns filed under the wrong basis: found by their numbers, refused by the gate."""

from __future__ import annotations

import dataclasses
import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from finrag.core.config import get_settings
from finrag.core.db import tables, transaction
from finrag.core.types import Period, PeriodType, SourceClass
from finrag.facts.basis_check import misbased_columns
from finrag.facts.current_view import get_fact
from finrag.facts.gap_gate import REASON_BASIS, run_gate
from finrag.facts.models import Fact

REAL = Path(__file__).resolve().parents[2]
Q1 = Period(date(2026, 4, 1), date(2026, 6, 30), PeriodType.Q)
# Gabriel Q1 FY27, millions: the standalone statement (page 4 of the PDF) names no basis
STANDALONE = {"profit_before_tax": "998.24", "tax_expense": "238.56", "total_income": "12929.26"}
CONSOLIDATED = {"profit_before_tax": "905.17", "tax_expense": "251.34", "total_income": "14303.12"}


def fact(concept: str, value: str, basis: str, src: SourceClass, filing: str, n: int = 0) -> Fact:
    return Fact(
        fact_id=f"FCT-{concept}-{basis}-{src.value}-{n}", isin="INE1", concept=concept, period=Q1,
        basis=basis, value=Decimal(value) * 1_000_000, unit="INR", source_filing_id=filing,
        parsed_fact_id=None, mapping_method=None, is_derived=False, derivation=None,
        stored_filed_on=None, created_at="2026-07-01", source_class=src, filed_on="2026-07-01",
        revision_no=0, confidence=0.9,
    )  # fmt: skip


def versions(pdf_values, pdf_basis="consolidated", extra=()):
    xbrl_c = [
        fact(c, v, "consolidated", SourceClass.XBRL, "FIL-x1") for c, v in CONSOLIDATED.items()
    ]
    xbrl_s = [fact(c, v, "standalone", SourceClass.XBRL, "FIL-x2") for c, v in STANDALONE.items()]
    pdf = [fact(c, v, pdf_basis, SourceClass.PDF, "FIL-pdf") for c, v in pdf_values.items()]
    out = {"consolidated": xbrl_c, "standalone": xbrl_s}
    out.setdefault(pdf_basis, []).extend(pdf)
    for f in extra:
        out[f.basis].append(f)
    return out


def test_a_column_that_matches_the_other_basis_is_found() -> None:
    found = misbased_columns(versions(STANDALONE), get_settings())
    assert found == {(("FIL-pdf", date(2026, 6, 30)), "consolidated"): "standalone"}


def test_a_column_that_matches_its_own_basis_is_left_alone() -> None:
    assert misbased_columns(versions(CONSOLIDATED), get_settings()) == {}


def test_too_few_shared_keys_is_not_enough() -> None:
    two = {k: STANDALONE[k] for k in ("profit_before_tax", "tax_expense")}
    assert misbased_columns(versions(two), get_settings()) == {}


def test_one_disagreeing_key_is_not_a_basis_mismatch() -> None:
    mixed = {**STANDALONE, "total_income": "14303.12"}  # a key that is the consolidated value
    assert misbased_columns(versions(mixed), get_settings()) == {}


def test_numbers_equal_on_both_bases_prove_nothing() -> None:
    same = {k: CONSOLIDATED[k] for k in CONSOLIDATED}
    v = versions(same)
    v["standalone"] = [
        fact(c, x, "standalone", SourceClass.XBRL, "FIL-x2") for c, x in same.items()
    ]
    assert misbased_columns(v, get_settings()) == {}


@pytest.fixture(autouse=True)
def gate_registry(isolated_repo: Path) -> None:
    shutil.copy(
        REAL / "config" / "validation_rules.yaml",
        isolated_repo / "config" / "validation_rules.yaml",
    )


def put(kit, concept: str, value: str, basis: str, filing: str, extractor: str, page: int | None):
    """A canonical fact for Q1 (via the normal writer), with its PDF page recorded."""
    c = kit.cand(filing, "Q1", str(Decimal(value) * 1_000_000), concept, extractor, 0.9)
    c = dataclasses.replace(c, basis=basis)
    if page is not None:
        with transaction() as conn:
            conn.execute(
                sa.update(tables.parsed_facts)
                .where(tables.parsed_facts.c.id == c.parsed_fact_id)
                .values(
                    source_loc=f"{page}:5:1",
                    statement_type="profit_and_loss",
                    period_start="2025-04-01",
                )
            )
    kit.write([c])


def test_the_gate_refuses_a_misbased_column_and_the_other_columns_on_its_page(
    kit: SimpleNamespace,
) -> None:
    kit.add_filing("FIL-x1")
    kit.add_filing("FIL-x2")
    kit.add_filing("FIL-pdf", fmt="pdf")
    for concept, value in CONSOLIDATED.items():
        put(kit, concept, value, "consolidated", "FIL-x1", "xbrl", None)
    for concept, value in STANDALONE.items():
        put(kit, concept, value, "standalone", "FIL-x2", "xbrl", None)
        put(
            kit, concept, value, "consolidated", "FIL-pdf", "ocr", 4
        )  # standalone page, mislabelled
    put(kit, "revenue", "5000", "consolidated", "FIL-pdf", "ocr", 4)  # another cell of the page
    put(kit, "revenue", "9000", "consolidated", "FIL-pdf", "ocr", 7)  # a genuine consolidated page
    report = run_gate(kit.ISIN)
    refused = {
        f["concept"]: f["reasons"]
        for col in report.decisions
        for f in json.loads(json.dumps(col.details(0.7)))["rejected_facts"]
    }
    assert {"profit_before_tax", "tax_expense", "total_income", "revenue"} <= set(refused)
    assert all(
        REASON_BASIS in refused[c] for c in ("profit_before_tax", "tax_expense", "total_income")
    )
    page7 = [c for c in report.decisions if any(f.page == 7 for f in c.facts)]
    assert page7 and not any(f.reasons for c in page7 for f in c.facts if f.page == 7)
    q1 = Period(date(2025, 4, 1), date(2025, 6, 30), PeriodType.Q)  # the fixture's FY26 Q1
    assert get_fact(kit.ISIN, "profit_before_tax", q1, "consolidated").value == Decimal("905170000")
