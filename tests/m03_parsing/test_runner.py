"""The parse runner: routing, writes, statuses, jobs rows, review items, idempotence, the CLI."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from finrag.cli import app
from finrag.core.db import tables, transaction
from finrag.parsing.runner import ParseError, run_parse

CLEAN = (
    "Revenue from operations increased because the company sold more products to customers "
    "during the quarter. Management expects demand to remain strong across the business and "
    "believes that margins will improve as costs come down over the next few quarters."
)


def rows(table, *where):
    with transaction() as conn:
        return list(
            conn.execute(
                sa.select(table).where(*where).order_by(table.c.id if "id" in table.c else None)
            )
        )


def facts_of(filing_id: str):
    return rows(tables.parsed_facts, tables.parsed_facts.c.filing_id == filing_id)


def filing(filing_id: str):
    return rows(tables.filings, tables.filings.c.filing_id == filing_id)[0]


def processed(isolated_repo, filing_id: str, name: str):
    return isolated_repo / "data" / "processed" / filing_id / name


def test_xbrl_filing_becomes_parsed_facts(kit, register, isolated_repo) -> None:
    filing_id = register(kit.xbrl.build_instance(), basis="standalone")
    (report,) = run_parse()
    assert (report.filing_id, report.status, report.error) == (filing_id, "parsed", None)
    expected = kit.xbrl.Expected()
    facts = facts_of(filing_id)
    assert len(facts) == expected.numeric + expected.nil
    assert report.stats["numeric_facts"] == len(facts)
    assert filing(filing_id).status == "parsed" and filing(filing_id).error is None
    assert {f.extractor for f in facts} == {"xbrl"}
    assert {f.basis for f in facts} == {"standalone"}
    assert {f.ocr_backend for f in facts} == {None}


def test_values_are_stored_as_exact_text(kit, register) -> None:
    filing_id = register(kit.xbrl.build_instance())
    run_parse()
    by_concept = {(f.concept_raw, f.source_loc.split(" ")[0]): f for f in facts_of(filing_id)}
    revenue = by_concept[("in-capmkt:RevenueFromOperations", "OneD")]
    assert revenue.value == "7737020000" and isinstance(revenue.value, str)
    assert Decimal(by_concept[("in-capmkt:BasicEarningsPerShare", "OneD")].value) == Decimal(
        "12.43"
    )
    assert by_concept[("in-capmkt:ExceptionalItems", "OneD")].value is None
    assert (revenue.scale, revenue.unit_raw, revenue.decimals) == (0, "iso4217:INR", -4)
    assert by_concept[("in-capmkt:BasicEarningsPerShare", "OneD")].decimals is None  # INF
    assert revenue.period_start == "2026-01-01" and revenue.period_end == "2026-03-31"


def test_dimensions_json_marks_non_totals(kit, register) -> None:
    filing_id = register(kit.xbrl.build_instance())
    run_parse()
    facts = facts_of(filing_id)
    dimensioned = [f for f in facts if f.dimensions_json is not None]
    assert len(dimensioned) == kit.xbrl.Expected().dimensioned
    other = next(f for f in dimensioned if f.concept_raw == "in-capmkt:OtherExpenses")
    assert json.loads(other.dimensions_json) == [
        {
            "axis": "in-capmkt:DetailsOfOtherExpensesAxis",
            "member": "in-capmkt:OtherExpenses1Member",
        }
    ]
    assert "OtherExpenses1Member" in other.source_loc
    totals = [f for f in facts if f.dimensions_json is None]
    assert all("[" not in f.source_loc for f in totals)


def test_text_facts_are_written_apart(kit, register, isolated_repo) -> None:
    filing_id = register(kit.xbrl.build_instance())
    run_parse()
    lines = (
        processed(isolated_repo, filing_id, "text_facts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    records = [json.loads(line) for line in lines]
    assert len(records) == kit.xbrl.Expected().text
    assert {r["concept"] for r in records} >= {"in-capmkt:ISIN", "in-capmkt:NameOfTheCompany"}
    assert not any(f.concept_raw == "in-capmkt:ISIN" for f in facts_of(filing_id))


def test_job_row_carries_counts(kit, register) -> None:
    filing_id = register(kit.xbrl.build_instance())
    run_parse()
    (job,) = rows(tables.jobs)
    assert (job.job_type, job.status, job.isin) == ("parse", "succeeded", kit.ISIN)
    stats = json.loads(job.stats_json)
    assert stats["numeric_facts"] == len(facts_of(filing_id))
    assert stats["text_facts"] == kit.xbrl.Expected().text


def test_results_pdf_gives_page_text_and_table_facts(kit, register, isolated_repo) -> None:
    filing_id = register(kit.results.build_pdf(), fmt="pdf")
    (report,) = run_parse(backend=kit.FakeOcr([]))
    assert report.status == "parsed"
    facts = facts_of(filing_id)
    assert len(facts) == len(kit.results.ROWS) * 5
    assert {f.extractor for f in facts} == {"pdf_table"}
    assert {f.statement_type for f in facts} == {"profit_and_loss"}  # from the page classifier
    assert report.stats["statement_pages"] == [1] and report.stats["table_pages_read"] == [1]
    pages = (
        processed(isolated_repo, filing_id, "pages.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert json.loads(pages[0])["method"] == "native"


def test_low_confidence_table_is_queued_once_per_page_and_facts_are_kept(kit, register) -> None:
    filing_id = register(kit.results.build_pdf(kind_labels=False), fmt="pdf")
    run_parse(backend=kit.FakeOcr([]))
    assert facts_of(filing_id)
    (item,) = rows(tables.review_queue, tables.review_queue.c.item_type == "low_confidence_table")
    payload = json.loads(item.payload_json)
    assert (payload["filing_id"], payload["page"], item.status) == (filing_id, 1, "open")
    run_parse(filing_id, backend=kit.FakeOcr([]))  # parse again: same page, same item
    (again,) = rows(tables.review_queue, tables.review_queue.c.item_type == "low_confidence_table")
    assert again.occurrences == 2


def test_reparse_replaces_facts_instead_of_duplicating(kit, register) -> None:
    filing_id = register(kit.xbrl.build_instance())
    run_parse()
    first = len(facts_of(filing_id))
    run_parse(filing_id)
    assert len(facts_of(filing_id)) == first
    assert filing(filing_id).status == "parsed"


def test_second_plain_run_has_nothing_to_do(kit, register) -> None:
    register(kit.xbrl.build_instance())
    run_parse()
    assert run_parse() == []


def test_annual_report_is_text_only(kit, register, isolated_repo) -> None:
    filing_id = register(
        kit.text_pdf([CLEAN, CLEAN]), fmt="pdf", filing_type="annual_report", basis="consolidated"
    )
    (report,) = run_parse(backend=kit.FakeOcr([]))
    assert report.stats["pages"] == 2 and report.stats["numeric_facts"] == 0
    assert "statement_pages" not in report.stats
    assert facts_of(filing_id) == []
    assert (
        len(processed(isolated_repo, filing_id, "pages.jsonl").read_text("utf-8").splitlines()) == 2
    )


def test_transcript_pages_keep_line_info(kit, register, isolated_repo) -> None:
    filing_id = register(
        kit.text_pdf([CLEAN]),
        fmt="pdf",
        filing_type="earnings_call_transcript",
        basis="consolidated",
    )
    run_parse(backend=kit.FakeOcr([]))
    record = json.loads(
        processed(isolated_repo, filing_id, "pages.jsonl").read_text("utf-8").splitlines()[0]
    )
    assert record["lines"] and {"text", "bold", "font", "size"} <= set(record["lines"][0])


def scan_of_results(kit):
    """(a PDF with no text layer, the OCR result a backend would give for the results page)."""
    import io

    import pdfplumber
    import pymupdf

    from finrag.parsing.pdf_tables import _native_words

    table_pdf = kit.results.build_pdf()
    with pdfplumber.open(io.BytesIO(table_pdf)) as doc:
        words = tuple(
            kit.OcrWord(w.text, w.x0, w.x1, w.top, w.bottom) for w in _native_words(doc.pages[0])
        )
    text = pymupdf.open(stream=table_pdf, filetype="pdf")[0].get_text()
    return kit.blank_pdf(), kit.ocr_result(text, words)


def test_ocr_facts_are_labelled_and_xbrl_rows_are_untouched(kit, register) -> None:
    xbrl_id = register(kit.xbrl.build_instance())
    run_parse()
    before = [(f.id, f.value, f.extractor) for f in facts_of(xbrl_id)]
    scan, result = scan_of_results(kit)
    pdf_id = register(scan, fmt="pdf")
    (report,) = run_parse(backend=kit.FakeOcr([result]))
    assert report.status == "parsed" and report.stats["ocr_pages"] == 1
    ocr_facts = facts_of(pdf_id)
    assert ocr_facts
    assert {(f.extractor, f.ocr_backend) for f in ocr_facts} == {("ocr", "tesseract")}
    assert {f.statement_type for f in ocr_facts} == {"profit_and_loss"}
    assert {f.statement_type for f in facts_of(xbrl_id)} == {None}  # XBRL facts have none
    assert [(f.id, f.value, f.extractor) for f in facts_of(xbrl_id)] == before


def test_quarantined_filings_are_never_parsed(kit, register) -> None:
    filing_id = register(kit.xbrl.build_instance(), status="quarantined")
    assert run_parse() == []
    with pytest.raises(ParseError, match="quarantined"):
        run_parse(filing_id)
    assert filing(filing_id).status == "quarantined"
    assert facts_of(filing_id) == []


def test_normalized_filing_is_not_reparsed(kit, register) -> None:
    filing_id = register(kit.xbrl.build_instance(), status="normalized")
    with pytest.raises(ParseError, match="already normalized"):
        run_parse(filing_id)


def test_unknown_filing_id(kit, register) -> None:
    register(kit.xbrl.build_instance())
    with pytest.raises(ParseError, match="unknown filing"):
        run_parse("FIL-doesnotexist")


def test_missing_raw_file_fails_that_filing_only(kit, register, isolated_repo) -> None:
    good = register(kit.xbrl.build_instance(), name="good")
    bad_data = kit.xbrl.build_instance().replace(b"7737020000", b"7737020001")
    bad = register(bad_data, name="bad")
    (isolated_repo / "data" / "raw" / kit.ISIN / "bad.xml").unlink()
    reports = {r.filing_id: r for r in run_parse()}
    assert reports[good].status == "parsed"
    assert reports[bad].status == "failed" and "raw file is missing" in reports[bad].error
    assert filing(bad).status == "failed" and "raw file is missing" in filing(bad).error
    statuses = {j.status for j in rows(tables.jobs)}
    assert statuses == {"succeeded", "failed"}


def test_raw_file_changed_after_registration_fails(kit, register, isolated_repo) -> None:
    filing_id = register(kit.xbrl.build_instance(), name="tampered")
    path = isolated_repo / "data" / "raw" / kit.ISIN / "tampered.xml"
    path.write_bytes(path.read_bytes() + b"\n<!-- changed -->")
    (report,) = run_parse()
    assert report.status == "failed" and "sha256" in report.error
    assert facts_of(filing_id) == []


def test_unparseable_xbrl_fails_with_the_reason(kit, register) -> None:
    filing_id = register(b"<not-xml", name="broken")
    (report,) = run_parse()
    assert report.status == "failed" and "not valid XML" in report.error
    assert filing(filing_id).status == "failed"


def test_cli_run_and_show(kit, register) -> None:
    filing_id = register(kit.xbrl.build_instance())
    runner = CliRunner()
    result = runner.invoke(app, ["parse", "run"])
    assert result.exit_code == 0, result.output
    assert f"{filing_id}: parsed" in result.output
    shown = runner.invoke(app, ["parse", "show", "--filing", filing_id, "--limit", "3"])
    assert shown.exit_code == 0
    assert "parsed facts for" in shown.output and "(showing 3)" in shown.output
    assert "in-capmkt:" in shown.output
    again = runner.invoke(app, ["parse", "run"])
    assert "nothing to parse" in again.output


def test_cli_reports_a_failed_filing_with_exit_code_1(kit, register) -> None:
    register(b"<not-xml", name="broken")
    result = CliRunner().invoke(app, ["parse", "run"])
    assert result.exit_code == 1
    assert "failed" in result.output and "error:" in result.output


def test_cli_quarantined_filing_exits_2(kit, register) -> None:
    filing_id = register(kit.xbrl.build_instance(), status="quarantined")
    result = CliRunner().invoke(app, ["parse", "run", "--filing", filing_id])
    assert result.exit_code == 2
