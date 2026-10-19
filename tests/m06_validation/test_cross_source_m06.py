"""cross_source: XBRL vs PDF/OCR, the restated-comparative vs OCR-error classifier, and events."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from finrag.cli import app
from finrag.core.db import tables, transaction
from finrag.core.types import ValidationStatus
from finrag.facts.current_view import get_fact
from finrag.normalize.review_queue import ReviewError, ignore, list_items
from finrag.validation.cross_source import (
    OCR_ERROR,
    RESTATED,
    UNCLEAR,
    decimal_shift,
    digit_substitution,
)
from finrag.validation.engine import run_validation, warnings_for
from finrag.validation.events import ITEM_EVENT_PROPOSAL, confirm_event, recorded_events

BASIS = "consolidated"
FY25 = ("2024-04-01", "2025-03-31")

# A small P&L (millions) as the XBRL filing has it, and as the later PDF has it after the company
# changed its inventory valuation: four lines move, the totals move with them, the sums hold.
XBRL_PL = {
    "revenue": "1000", "other_income": "10", "total_income": "1010",
    "cost_of_materials": "600", "change_in_inventories": "-20", "employee_expense": "100",
    "finance_cost": "10", "depreciation_amortisation": "20", "other_expenses": "90",
    "total_expenses": "800", "profit_before_tax": "210", "tax_expense": "50",
    "profit_after_tax": "160",
}  # fmt: skip
RESTATED_PL = {
    **XBRL_PL,
    "cost_of_materials": "604.12", "change_in_inventories": "-14.44", "employee_expense": "102.10",
    "other_expenses": "87.90", "total_expenses": "809.68", "profit_before_tax": "200.32",
    "tax_expense": "47.56", "profit_after_tax": "152.76",
}  # fmt: skip


def write(kit, values: dict[str, str], src: str, scale: int = 1, **kw) -> None:
    """Write a P&L (millions x scale) for FY25 from one source."""
    for concept, value in values.items():
        kit.put(concept, *FY25, kit.mn(str(Decimal(value) * scale)), "FY", src=src, **kw)


def run(kit):
    """Run validation -> (report, the xbrl_vs_pdf row of FY25)."""
    report = run_validation(kit.ISIN)
    rows = [r for r in report.results if r.rule_id == "xbrl_vs_pdf" and r.period_type == "FY"]
    return report, (rows[0] if rows else None)


def count(table) -> int:
    with transaction() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar()


def proposals():
    return list_items(None, ITEM_EVENT_PROPOSAL)


def test_restated_comparative_below_tolerance_is_classified_not_warned(kit) -> None:
    write(kit, XBRL_PL, "xbrl")
    write(kit, RESTATED_PL, "ocr", conf=0.95)
    report, row = run(kit)
    assert row.status is ValidationStatus.PASS  # 9.68 mn is under the 10 mn absolute tolerance
    assert row.details["compared"] == len(XBRL_PL)
    assert len(row.details["differences"]) == 8  # only the lines that moved
    cluster = row.details["clusters"][0]
    assert cluster["classification"] == RESTATED
    assert cluster["differing_keys"] == 8
    assert any("shift together" in r for r in cluster["reasons"])
    holds = {(e["identity"], e["pdf_holds"], e["xbrl_holds"]) for e in cluster["identities"]}
    assert ("profit_before_tax", True, True) in holds
    assert ("total_expenses", True, True) in holds
    assert len(report.proposals) == 1  # proposed all the same: the classifier decides this


def test_restated_comparative_beyond_tolerance_warns_and_proposes_an_event(kit) -> None:
    write(kit, XBRL_PL, "xbrl", scale=10)
    write(kit, RESTATED_PL, "ocr", scale=10, conf=0.95)
    facts_before, restatements_before = count(tables.canonical_facts), count(tables.restatements)
    report, row = run(kit)
    assert row.status is ValidationStatus.WARN
    assert row.details["clusters"][0]["classification"] == RESTATED
    assert "profit_before_tax" in row.details["concepts"]
    fy25 = kit.period(*FY25, "FY")
    assert [w["rule_id"] for w in warnings_for(kit.ISIN, "profit_before_tax", fy25, BASIS)] == [
        "xbrl_vs_pdf"
    ]
    # a finding only: the fact store is untouched and XBRL stays the current value
    assert (count(tables.canonical_facts), count(tables.restatements)) == (
        facts_before, restatements_before,
    )  # fmt: skip
    current = get_fact(kit.ISIN, "profit_before_tax", fy25, BASIS)
    assert current.value == kit.mn("2100")
    [item] = proposals()
    payload = json.loads(item.payload_json)
    assert report.proposals == [item.id]
    assert payload["proposed_event_type"] == "comparative_restatement"
    assert payload["proposed_event_date"] == "2025-04-01"
    assert payload["restated_period_end"] == "2025-03-31"
    assert {k["concept"] for k in payload["affected_keys"]} >= {
        "profit_before_tax",
        "total_expenses",
    }
    assert payload["classification"] == RESTATED and payload["reasons"] and payload["identities"]


def test_ocr_slip_in_one_line_is_an_ocr_error(kit) -> None:
    write(kit, XBRL_PL, "xbrl")
    write(kit, {k: v for k, v in XBRL_PL.items() if k != "profit_before_tax"}, "ocr", conf=0.95)
    kit.put(
        "profit_before_tax", *FY25, kit.mn("21000"), "FY", src="ocr", conf=0.3
    )  # 210.00 read as 21000
    _, row = run(kit)
    assert row.status is ValidationStatus.WARN
    [cluster] = row.details["clusters"]
    assert cluster["classification"] == OCR_ERROR
    assert "decimal_shift" in cluster["reasons"][0]
    assert {e["identity"] for e in cluster["identities"] if e["pdf_holds"] is False} == {
        "profit_before_tax", "profit_after_tax",
    }  # fmt: skip
    [diff] = row.details["differences"]
    assert set(diff["signals"]) == {"decimal_shift", "low_confidence"}
    assert not proposals()  # an OCR error proposes no corporate event


def test_isolated_line_without_an_ocr_pattern_is_unclear(kit) -> None:
    kit.put("total_assets", *("2025-03-31",) * 2, kit.mn("8933.6"))
    kit.put("total_assets", *("2025-03-31",) * 2, kit.mn("8950"), src="ocr", conf=0.9)
    report = run_validation(kit.ISIN)
    [row] = [r for r in report.results if r.rule_id == "xbrl_vs_pdf"]
    assert row.status is ValidationStatus.WARN  # 16.4 mn and 0.18 %: beyond both tolerances
    assert row.details["clusters"][0]["classification"] == UNCLEAR
    assert not proposals()


def test_several_lines_with_a_broken_sum_are_unclear(kit) -> None:
    write(kit, XBRL_PL, "xbrl")
    moved = {**XBRL_PL, "revenue": "1005", "other_income": "12", "cost_of_materials": "590"}
    write(kit, moved, "ocr", conf=0.95)  # 1005 + 12 != 1010: the PDF does not add up
    _, row = run(kit)
    cluster = row.details["clusters"][0]
    assert cluster["classification"] == UNCLEAR
    assert any("identities break" in r for r in cluster["reasons"])
    assert not proposals()


def test_rounding_differences_are_not_findings(kit) -> None:
    write(kit, XBRL_PL, "xbrl")
    rounded = {**XBRL_PL, "revenue": "1000.004", "total_income": "1010.004"}  # 4,000 rupees
    write(kit, rounded, "ocr", conf=0.95)
    _, row = run(kit)
    assert (row.status, row.details["differences"], "clusters" in row.details) == (
        ValidationStatus.PASS, [], False,
    )  # fmt: skip


def test_nothing_to_compare_gives_no_row(kit) -> None:
    write(kit, XBRL_PL, "xbrl")
    report = run_validation(kit.ISIN)
    assert not [r for r in report.results if r.rule_id == "xbrl_vs_pdf"]


def test_pdf_only_comparative_has_no_xbrl_to_differ_from(kit) -> None:
    write(kit, RESTATED_PL, "ocr", conf=0.95)
    assert not [r for r in run_validation(kit.ISIN).results if r.rule_id == "xbrl_vs_pdf"]


# --- events ------------------------------------------------------------------------------------


def restated(kit) -> int:
    """Set up a restated comparative and run validation -> the proposal's item id."""
    write(kit, XBRL_PL, "xbrl", scale=10)
    write(kit, RESTATED_PL, "ocr", scale=10, conf=0.95)
    [item] = run_validation(kit.ISIN).proposals
    return item


def test_confirming_a_proposal_records_the_event(kit) -> None:
    item = restated(kit)
    assert recorded_events(kit.ISIN) == []
    event_id = confirm_event(
        item, "accounting_policy_change", citation="Q4 FY26 results PDF, Note 5"
    )
    [event] = recorded_events(kit.ISIN)
    assert (event.id, event.event_type, event.event_date) == (
        event_id, "accounting_policy_change", "2025-04-01",
    )  # fmt: skip
    details = json.loads(event.details)
    assert details["citation"] == "Q4 FY26 results PDF, Note 5"
    assert details["basis"] == BASIS and details["pdf_filing_id"] == "FIL-p"
    assert {k["concept"] for k in details["affected_keys"]} >= {"profit_before_tax"}
    [resolved] = list_items("resolved", ITEM_EVENT_PROPOSAL)
    assert json.loads(resolved.resolution_json)["event_id"] == event_id
    # confirming changes no fact either
    fy25 = kit.period(*FY25, "FY")
    assert get_fact(kit.ISIN, "profit_before_tax", fy25, BASIS).value == kit.mn("2100")


def test_default_event_type_is_comparative_restatement(kit) -> None:
    confirm_event(restated(kit))
    assert recorded_events(kit.ISIN)[0].event_type == "comparative_restatement"


def test_a_decided_proposal_is_not_queued_again(kit) -> None:
    item = restated(kit)
    run_validation(kit.ISIN)  # still open: same item, one more occurrence
    [open_item] = proposals()
    assert (open_item.id, open_item.occurrences) == (item, 2)
    confirm_event(item)
    assert run_validation(kit.ISIN).proposals == []
    assert len(proposals()) == 1


def test_an_ignored_proposal_is_not_queued_again(kit) -> None:
    item = restated(kit)
    ignore(item)
    assert run_validation(kit.ISIN).proposals == []
    assert recorded_events(kit.ISIN) == []


def test_confirm_errors(kit) -> None:
    item = restated(kit)
    with pytest.raises(ReviewError, match="no review item"):
        confirm_event(999)
    with pytest.raises(ReviewError, match="event type must be one of"):
        confirm_event(item, "merger")
    confirm_event(item)
    with pytest.raises(ReviewError, match="already resolved"):
        confirm_event(item)
    with transaction() as conn:
        other = conn.execute(
            sa.insert(tables.review_queue).values(
                item_type="entity_ambiguous", payload_json="{}", occurrences=1, status="open"
            )
        ).inserted_primary_key[0]
    with pytest.raises(ReviewError, match="not a corporate_event_proposal"):
        confirm_event(other)


def test_a_cluster_already_explained_by_a_recorded_event_is_not_proposed_again(kit) -> None:
    """The decided item is lost (or the validation re-run): the event still explains the cluster."""
    first = restated(kit)
    event_id = confirm_event(first, "accounting_policy_change")
    with transaction() as conn:
        conn.execute(sa.delete(tables.review_queue).where(tables.review_queue.c.id == first))
    assert run_validation(kit.ISIN).proposals == []
    assert list_items("open", ITEM_EVENT_PROPOSAL) == []
    assert [e.id for e in recorded_events(kit.ISIN)] == [event_id]


def test_another_filing_showing_the_same_restatement_is_attached_to_the_event(kit) -> None:
    """Netweb: the Q4 FY26 results and, later, the Q2 FY26 results both carry the restated FY25."""
    first = restated(kit)
    event_id = confirm_event(first, "accounting_policy_change", citation="Note 5")
    write(kit, RESTATED_PL, "ocr", scale=10, conf=0.95, filing="FIL-p2")
    assert run_validation(kit.ISIN).proposals == []  # no second proposal
    assert list_items("open", ITEM_EVENT_PROPOSAL) == []
    [event] = recorded_events(kit.ISIN)
    linked = json.loads(event.details)["linked_filings"]
    assert [(x["pdf_filing_id"], x["period_end"]) for x in linked] == [("FIL-p2", "2025-03-31")]
    assert event.id == event_id
    run_validation(kit.ISIN)  # attaching is idempotent
    assert len(json.loads(recorded_events(kit.ISIN)[0].details)["linked_filings"]) == 1


def test_an_open_proposal_of_the_same_restatement_is_resolved_by_the_event(kit) -> None:
    write(kit, XBRL_PL, "xbrl", scale=10)
    write(kit, RESTATED_PL, "ocr", scale=10, conf=0.95)
    write(kit, RESTATED_PL, "ocr", scale=10, conf=0.95, filing="FIL-p2")
    proposals = run_validation(kit.ISIN).proposals
    assert len(proposals) == 2  # no event recorded yet: both filings are proposed
    confirm_event(proposals[0], "accounting_policy_change")
    run_validation(kit.ISIN)
    [resolved] = [i for i in list_items("resolved", ITEM_EVENT_PROPOSAL) if i.id == proposals[1]]
    assert json.loads(resolved.resolution_json)["how"].startswith("same restatement")
    assert list_items("open", ITEM_EVENT_PROPOSAL) == []


def test_a_pdf_column_filed_under_the_wrong_basis_is_compared_with_the_right_one(kit) -> None:
    """Gabriel Q1 FY27: the standalone statement printed without a basis word is labelled
    consolidated; its numbers are the standalone XBRL ones. Not a restatement, no proposal."""
    write(kit, XBRL_PL, "xbrl", scale=10)  # consolidated
    write(kit, RESTATED_PL, "xbrl", scale=10, basis="standalone", filing="FIL-xs")
    write(kit, RESTATED_PL, "ocr", scale=10, conf=0.95)  # labelled consolidated
    report = run_validation(kit.ISIN)
    assert report.proposals == []
    rows = {
        (r.basis): r for r in report.results if r.rule_id == "xbrl_vs_pdf" and r.period_type == "FY"
    }
    assert "consolidated" not in rows  # nothing of the PDF is left to compare on that basis
    assert rows["standalone"].status is ValidationStatus.PASS
    assert rows["standalone"].details["basis_corrected"] == [
        {"pdf_filing": "FIL-p", "labelled": "consolidated", "values_match": "standalone"}
    ]


def test_a_different_restatement_of_the_same_period_is_still_proposed(kit) -> None:
    first = restated(kit)
    confirm_event(first, "accounting_policy_change")
    other = {  # a different restatement that still adds up
        **RESTATED_PL,
        "other_expenses": "89.22",
        "total_expenses": "811.00",
        "profit_before_tax": "199.00",
        "profit_after_tax": "151.44",
    }
    write(kit, other, "ocr", scale=10, conf=0.95, filing="FIL-p3")
    assert len(run_validation(kit.ISIN).proposals) == 1


def test_link_event_command_closes_a_proposal_by_hand(kit) -> None:
    item = restated(kit)
    with transaction() as conn:
        event_id = conn.execute(
            sa.insert(tables.corporate_events).values(
                isin=kit.ISIN,
                event_type="accounting_policy_change",
                event_date="2025-04-01",
                details="{}",
                source="test",
            )  # fmt: skip
        ).inserted_primary_key[0]
    result = CliRunner().invoke(app, ["review", "link-event", str(item), "--event", str(event_id)])
    assert result.exit_code == 0 and f"linked to corporate event {event_id}" in result.output
    [resolved] = list_items("resolved", ITEM_EVENT_PROPOSAL)
    assert json.loads(resolved.resolution_json)["linked_event"] == event_id


def test_confirm_the_same_event_twice_is_refused(kit) -> None:
    first = restated(kit)
    confirm_event(first, "accounting_policy_change")
    with transaction() as conn:  # a proposal for another filing, same effective date
        other = conn.execute(
            sa.insert(tables.review_queue).values(
                item_type=ITEM_EVENT_PROPOSAL,
                payload_json=json.dumps(
                    {
                        "isin": kit.ISIN,
                        "basis": BASIS,
                        "filing_id": "FIL-z",
                        "restated_period_end": "2025-03-31",
                        "proposed_event_type": "comparative_restatement",
                        "proposed_event_date": "2025-04-01",
                        "rule_id": "xbrl_vs_pdf",
                        "classification": RESTATED,
                        "affected_keys": [],
                    }
                ),
                occurrences=1,
                status="open",
            )  # fmt: skip
        ).inserted_primary_key[0]
    with pytest.raises(ReviewError, match="already recorded"):
        confirm_event(other, "accounting_policy_change")


# --- patterns and the command line --------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("948.22", "94822", True),
        ("94822", "948.22", True),
        ("948.22", "9482.2", True),
        ("948.22", "948220000", False),  # 10^6 is beyond decimal_shift_max_power
        ("948.22", "948.22", False),
        ("948.22", "0", False),
    ],
)
def test_decimal_shift(a: str, b: str, expected: bool) -> None:
    assert decimal_shift(Decimal(a), Decimal(b), 3) is expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("8933.60", "8938.60", True),  # one digit
        ("8933.60", "8393.60", True),  # adjacent swap
        ("8933.60", "8922.09", False),  # several digits: a different number
        ("1539.68", "1530.00", False),
        ("8933.60", "8933.60", False),
        ("8933.60", "89336.00", False),  # different length
    ],
)
def test_digit_substitution(a: str, b: str, expected: bool) -> None:
    unit = Decimal(10_000)
    assert digit_substitution(Decimal(a) * 10**6, Decimal(b) * 10**6, unit) is expected


def test_command_line_run_show_and_confirm(kit) -> None:
    runner = CliRunner()
    item = restated(kit)
    out = runner.invoke(app, ["validate", "run", "--isin", kit.ISIN])
    assert out.exit_code == 0, out.output
    assert "warn" in out.output and f"review item {item}" in out.output
    shown = runner.invoke(app, ["validate", "show", "--isin", kit.ISIN, "--period", "FY25"])
    assert "xbrl_vs_pdf" in shown.output and RESTATED in shown.output
    listed = runner.invoke(app, ["review", "list", "--type", ITEM_EVENT_PROPOSAL])
    assert "restates 8 lines" in listed.output
    done = runner.invoke(
        app, ["review", "confirm-event", str(item), "--event-type", "accounting_policy_change",
              "--event-date", "2025-04-01", "--citation", "Note 5"],
    )  # fmt: skip
    assert done.exit_code == 0 and "corporate event" in done.output
    bad = runner.invoke(app, ["review", "confirm-event", str(item)])
    assert bad.exit_code == 2 and "already resolved" in bad.output
