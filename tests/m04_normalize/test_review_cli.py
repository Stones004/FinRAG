"""Review-queue helpers and the `finrag review` / `finrag normalize` commands."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from finrag.cli import app
from finrag.normalize import review_queue
from finrag.normalize.runner import run_normalize

runner = CliRunner()


def queue_one(kit, taxonomy, writer) -> int:
    kit.add_filing()
    kit.add_fact("1", "in-capmkt:NeverSeenBefore")
    run_normalize(writer, taxonomy=taxonomy)
    return review_queue.list_items(None, "unmapped_concept")[0].id


def test_resolve_validates_concept_and_state(kit, taxonomy, writer) -> None:
    item_id = queue_one(kit, taxonomy, writer)
    with pytest.raises(review_queue.ReviewError, match="unknown concept"):
        review_queue.resolve(item_id, "nope", taxonomy)
    with pytest.raises(review_queue.ReviewError, match="no review item"):
        review_queue.resolve(999, "revenue", taxonomy)
    review_queue.resolve(item_id, "revenue", taxonomy)
    with pytest.raises(review_queue.ReviewError, match="already resolved"):
        review_queue.resolve(item_id, "cfo", taxonomy)
    with pytest.raises(review_queue.ReviewError, match="already resolved"):
        review_queue.ignore(item_id)


def test_only_unmapped_concept_items_can_be_resolved(kit, taxonomy, writer) -> None:
    kit.add_filing(basis="unknown")
    kit.add_fact("1", "in-capmkt:RevenueFromOperations", basis="unknown")
    run_normalize(writer, taxonomy=taxonomy)
    (item,) = review_queue.list_items(None, "ambiguous_basis")
    with pytest.raises(review_queue.ReviewError, match="only unmapped_concept"):
        review_queue.resolve(item.id, "revenue", taxonomy)
    review_queue.ignore(item.id)  # any open item can be ignored
    assert review_queue.list_items("ignored")[0].id == item.id


def test_resolutions_are_loaded_by_key(kit, taxonomy, writer) -> None:
    item_id = queue_one(kit, taxonomy, writer)
    review_queue.resolve(item_id, "cfo", taxonomy)
    found = review_queue.load_resolutions()
    assert found.qnames == {("indas", "in-capmkt:NeverSeenBefore"): "cfo"}


def test_review_commands(kit, taxonomy_file, taxonomy, writer) -> None:
    item_id = queue_one(kit, taxonomy, writer)
    listed = runner.invoke(app, ["review", "list"])
    assert listed.exit_code == 0 and "in-capmkt:NeverSeenBefore" in listed.output
    assert f"{item_id}" in listed.output and "unmapped_concept" in listed.output

    bad = runner.invoke(app, ["review", "resolve", str(item_id), "--concept", "nope"])
    assert bad.exit_code == 2 and "unknown concept" in bad.output

    good = runner.invoke(app, ["review", "resolve", str(item_id), "--concept", "revenue"])
    assert good.exit_code == 0 and "resolved to revenue" in good.output
    assert "no review items" in runner.invoke(app, ["review", "list"]).output
    resolved = runner.invoke(app, ["review", "list", "--status", "resolved"])
    assert "NeverSeenBefore" in resolved.output

    again = runner.invoke(app, ["review", "ignore", str(item_id)])
    assert again.exit_code == 2 and "already resolved" in again.output


def test_run_command_says_why_it_cannot_run_without_m05(kit, taxonomy_file) -> None:
    result = runner.invoke(app, ["normalize", "run"])
    assert result.exit_code == 2 and "fact store (M05" in result.output


def test_report_for_unknown_isin_fails_cleanly(kit, taxonomy_file) -> None:
    result = runner.invoke(app, ["normalize", "report", "--isin", "INE999Z01017"])
    assert result.exit_code == 2 and "unknown entity" in result.output


def test_payload_records_the_example(kit, taxonomy, writer) -> None:
    item_id = queue_one(kit, taxonomy, writer)
    row = next(r for r in review_queue.list_items(None) if r.id == item_id)
    payload = json.loads(row.payload_json)
    assert payload["example"]["value"] == "1" and payload["filing_id"] == kit.FILING
