"""A restated comparative becomes a corporate event only if it is explained.

A native-text PDF column of an earlier period than its filing's own (a comparative) that differs
from the original in XBRL is a restatement when the arithmetic of the PDF still holds, or when the
filing's own note says the comparatives were restated and several lines shifted together. Any other
disagreement goes to the review queue and records nothing. A scan is never a comparative.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pymupdf
import pytest
import sqlalchemy as sa

from finrag.core.db import tables, transaction
from finrag.normalize.review_queue import ReviewError, list_items
from finrag.validation.cross_source import EXPLAINED_BY_IDENTITIES, EXPLAINED_BY_NOTE
from finrag.validation.engine import run_validation
from finrag.validation.events import (
    ITEM_DISAGREEMENT,
    ITEM_EVENT_PROPOSAL,
    confirm_event,
    link_item,
    recorded_events,
)
from finrag.validation.restatement_notes import find_note
from finrag.validation.rules import CrossSourceRule, load_rules

FY25 = ("2024-04-01", "2025-03-31")
SENTENCE = (
    "The Scheme was given effect to using the pooling of interest method, applied retrospectively "
    "for all periods presented, and the figures for the previous periods have been restated "
    "accordingly."
)

# a P&L (millions) as XBRL filed it; the PDF of the next year prints the lines again, restated
ORIGINAL = {
    "revenue": "1000", "other_income": "10", "total_income": "1010",
    "cost_of_materials": "600", "employee_expense": "100", "other_expenses": "90",
    "total_expenses": "790", "profit_before_tax": "220", "tax_expense": "50",
    "profit_after_tax": "170",
}  # fmt: skip
# every line shifted (no figure differs from its original by a single digit, which would read as
# an OCR slip), the sums still hold
RESTATED = {
    "revenue": "1137", "other_income": "12.4", "total_income": "1149.4",
    "cost_of_materials": "683", "employee_expense": "118", "other_expenses": "97.6",
    "total_expenses": "898.6", "profit_before_tax": "250.8", "tax_expense": "59.3",
    "profit_after_tax": "191.5",
}  # fmt: skip
# the lines shift but the sums do not hold (profit before tax is not income less expenses)
BROKEN = {**RESTATED, "profit_before_tax": "300", "profit_after_tax": "240.7"}


def write(kit, values: dict[str, str], src: str, filing: str | None = None, **kw) -> None:
    for concept, value in values.items():
        kit.put(concept, *FY25, kit.mn(value), "FY", src=src, filing=filing, **kw)


def pdf_with(tmp_path, text: str, name: str = "results.pdf") -> str:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(40, 40, 550, 300), text, fontsize=9)
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return str(path)


def point_filing_at(filing_id: str, raw_path: str) -> None:
    with transaction() as conn:
        conn.execute(
            sa.update(tables.filings)
            .where(tables.filings.c.filing_id == filing_id)
            .values(raw_path=raw_path, format="pdf")
        )


def open_items(item_type: str) -> list:
    return list_items("open", item_type)


# --- the explanations ------------------------------------------------------------------------


def test_comparative_whose_sums_hold_is_proposed_as_an_event_by_its_identities(kit) -> None:
    write(kit, ORIGINAL, "xbrl")
    write(kit, RESTATED, "pdf", conf=0.95)
    run_validation(kit.ISIN)
    (item,) = open_items(ITEM_EVENT_PROPOSAL)
    payload = json.loads(item.payload_json)
    assert payload["explained_by"] == [EXPLAINED_BY_IDENTITIES]
    assert payload["note"] is None
    assert open_items(ITEM_DISAGREEMENT) == []


def test_comparative_that_breaks_the_sums_with_no_note_goes_to_the_review_queue(kit) -> None:
    write(kit, ORIGINAL, "xbrl")
    write(kit, BROKEN, "pdf", conf=0.95)
    run_validation(kit.ISIN)
    assert open_items(ITEM_EVENT_PROPOSAL) == []
    (item,) = open_items(ITEM_DISAGREEMENT)
    payload = json.loads(item.payload_json)
    assert payload["filing_id"] == "FIL-p" and payload["restated_period_end"] == FY25[1]
    assert {d["concept"] for d in payload["differences"]} >= {"revenue", "profit_before_tax"}
    assert recorded_events(kit.ISIN) == []


def test_the_filings_own_note_explains_a_comparative_whose_sums_do_not_hold(kit, tmp_path) -> None:
    write(kit, ORIGINAL, "xbrl")
    write(kit, BROKEN, "pdf", conf=0.95)
    point_filing_at("FIL-p", pdf_with(tmp_path, f"Notes to the results. {SENTENCE} Other text."))
    run_validation(kit.ISIN)
    assert open_items(ITEM_DISAGREEMENT) == []
    (item,) = open_items(ITEM_EVENT_PROPOSAL)
    payload = json.loads(item.payload_json)
    assert payload["explained_by"] == [EXPLAINED_BY_NOTE]
    assert payload["note"]["page"] == 1 and payload["note"]["text"] == SENTENCE


def test_a_note_does_not_explain_one_isolated_line(kit, tmp_path) -> None:
    """A restatement shifts lines together; one odd cell is a misread, whatever the note says."""
    write(kit, ORIGINAL, "xbrl")
    write(kit, {**ORIGINAL, "revenue": "1100", "total_income": "1110"}, "pdf", conf=0.95)
    point_filing_at("FIL-p", pdf_with(tmp_path, SENTENCE))
    run_validation(kit.ISIN)
    assert open_items(ITEM_EVENT_PROPOSAL) == []
    assert len(open_items(ITEM_DISAGREEMENT)) == 1


def test_a_note_does_not_explain_lines_with_an_ocr_error_pattern(kit, tmp_path) -> None:
    """Lines 10x the original are a dropped decimal point, not a restatement."""
    write(kit, ORIGINAL, "xbrl")
    write(kit, {k: str(Decimal(v) * 10) for k, v in ORIGINAL.items()}, "pdf", conf=0.95)
    point_filing_at("FIL-p", pdf_with(tmp_path, SENTENCE))
    run_validation(kit.ISIN)
    assert open_items(ITEM_EVENT_PROPOSAL) == []


def test_a_scan_is_never_a_comparative_so_it_queues_no_disagreement(kit, tmp_path) -> None:
    write(kit, ORIGINAL, "xbrl")
    write(kit, BROKEN, "ocr", filing="FIL-scan", conf=0.95)
    point_filing_at("FIL-scan", pdf_with(tmp_path, SENTENCE))
    run_validation(kit.ISIN)
    assert open_items(ITEM_DISAGREEMENT) == [] and open_items(ITEM_EVENT_PROPOSAL) == []


def test_a_disagreement_that_was_decided_is_not_queued_again(kit) -> None:
    write(kit, ORIGINAL, "xbrl")
    write(kit, BROKEN, "pdf", conf=0.95)
    run_validation(kit.ISIN)
    (item,) = open_items(ITEM_DISAGREEMENT)
    with transaction() as conn:
        conn.execute(
            sa.update(tables.review_queue)
            .where(tables.review_queue.c.id == item.id)
            .values(status="ignored")
        )
    run_validation(kit.ISIN)
    assert open_items(ITEM_DISAGREEMENT) == []


# --- events ----------------------------------------------------------------------------------


def queue_unexplained_proposal(kit) -> int:
    """A proposal as an older run could have left it: neither identities nor a note recorded."""
    payload = {
        "isin": kit.ISIN, "basis": "consolidated", "filing_id": "FIL-p",
        "restated_period_end": FY25[1], "proposed_event_type": "comparative_restatement",
        "proposed_event_date": "2025-04-01", "rule_id": "xbrl_vs_pdf", "classification": "unclear",
        "affected_keys": [],
    }  # fmt: skip
    with transaction() as conn:
        result = conn.execute(
            sa.insert(tables.review_queue).values(
                item_type=ITEM_EVENT_PROPOSAL,
                payload_json=json.dumps(payload),
                occurrences=1,
                status="open",
                created_at="2026-01-01",
                updated_at="2026-01-01",
            )
        )
    return int(result.inserted_primary_key[0])


def test_an_event_is_not_recorded_without_a_note_or_identities(kit) -> None:
    item = queue_unexplained_proposal(kit)
    with pytest.raises(ReviewError, match="neither a restatement note nor identities"):
        confirm_event(item)
    assert recorded_events(kit.ISIN) == []


def test_a_scheme_restates_both_statements_one_event_covers_both_bases(kit, tmp_path) -> None:
    for basis in ("consolidated", "standalone"):
        write(kit, ORIGINAL, "xbrl", basis=basis)
        write(kit, RESTATED, "pdf", conf=0.95, basis=basis)
    run_validation(kit.ISIN)
    proposals = open_items(ITEM_EVENT_PROPOSAL)
    assert {json.loads(p.payload_json)["basis"] for p in proposals} == {
        "consolidated",
        "standalone",
    }
    first, second = sorted(proposals, key=lambda p: json.loads(p.payload_json)["basis"])
    event_id = confirm_event(first.id, citation="Scheme of arrangement")
    link_item(second.id, event_id)
    (event,) = recorded_events(kit.ISIN)
    details = json.loads(event.details)
    assert details["bases"] == ["consolidated", "standalone"]
    assert details["explained_by"] == [EXPLAINED_BY_IDENTITIES]
    # nothing is left to decide, and a later run queues nothing new for the same restatement
    run_validation(kit.ISIN)
    assert open_items(ITEM_EVENT_PROPOSAL) == []


# --- the note finder -------------------------------------------------------------------------


def patterns() -> list[str]:
    rule = load_rules().get("xbrl_vs_pdf")
    assert isinstance(rule, CrossSourceRule)
    return rule.restatement_notes


def test_find_note_returns_the_sentence_and_its_page(kit, tmp_path) -> None:
    kit.put("revenue", *FY25, kit.mn("1"), "FY", src="pdf")
    point_filing_at("FIL-p", pdf_with(tmp_path, f"A heading. {SENTENCE} Something else follows."))
    note = find_note("FIL-p", patterns())
    assert note is not None and (note.page, note.text) == (1, SENTENCE)


def test_find_note_prefers_the_sentence_that_names_the_basis(kit, tmp_path) -> None:
    standalone = SENTENCE.replace(
        "periods presented", "periods presented in the standalone results"
    )
    consolidated = standalone.replace("standalone", "consolidated")
    doc = pymupdf.open()
    for text in (standalone, consolidated):
        doc.new_page().insert_textbox(pymupdf.Rect(40, 40, 550, 300), text, fontsize=9)
    path = tmp_path / "two.pdf"
    doc.save(path)
    doc.close()
    kit.put("revenue", *FY25, kit.mn("1"), "FY", src="pdf")
    point_filing_at("FIL-p", str(path))
    assert find_note("FIL-p", patterns(), "consolidated").page == 2
    assert find_note("FIL-p", patterns(), "standalone").page == 1
    assert find_note("FIL-p", patterns()).page == 1


@pytest.mark.parametrize(
    "text",
    [
        "Previous period figures have been regrouped wherever necessary.",  # presentation only
        "The results were reviewed by the auditors.",
    ],
)
def test_find_note_ignores_text_that_does_not_say_comparatives_were_restated(
    kit, tmp_path, text
) -> None:
    kit.put("revenue", *FY25, kit.mn("1"), "FY", src="pdf")
    point_filing_at("FIL-p", pdf_with(tmp_path, text))
    assert find_note("FIL-p", patterns()) is None


def test_find_note_reads_only_native_text_pdfs(kit, tmp_path) -> None:
    kit.put("revenue", *FY25, kit.mn("1"), "FY", src="pdf")
    # a page that is a picture has no text layer to search
    doc = pymupdf.open()
    doc.new_page()
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    point_filing_at("FIL-p", str(path))
    assert find_note("FIL-p", patterns()) is None
    assert find_note("FIL-missing", patterns()) is None
    with transaction() as conn:
        conn.execute(sa.update(tables.filings).values(format="xbrl"))
    assert find_note("FIL-p", patterns()) is None
