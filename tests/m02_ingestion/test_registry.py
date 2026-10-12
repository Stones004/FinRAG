import json
from datetime import date

import pytest
import sqlalchemy as sa

from finrag.core.db import tables, transaction
from finrag.core.types import Basis, FilingFormat, FilingStatus, PeriodType
from finrag.ingestion import registry
from finrag.ingestion.models import Conflict, FilingCandidate
from finrag.ingestion.raw_store import store_raw
from finrag.ingestion.registry import DbEntityLookup, RawFile

CONFLICT = Conflict("period_type", "Q", "Half Yearly", "file says Half Yearly")


def cand(kit, **kw) -> FilingCandidate:
    base = dict(
        identifier=kit.ALPHA, isin=kit.ALPHA, source="local_inbox", filing_type="financial_results",
        format=FilingFormat.XBRL, basis=Basis.CONSOLIDATED, period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31), period_type=PeriodType.Q, filed_on="2026-05-02",
    )  # fmt: skip
    return FilingCandidate(**{**base, **kw})


def add(kit, data: bytes, conflicts=(), **kw):
    candidate = cand(kit, **kw)
    filing_id, path, sha = store_raw(data, candidate, kit.ALPHA)
    return registry.register(candidate, RawFile(filing_id, path, sha), conflicts)


def table_rows(table, *where):
    with transaction() as conn:
        return list(conn.execute(sa.select(table).where(*where)))


def filing(filing_id: str):
    (row,) = table_rows(tables.filings, tables.filings.c.filing_id == filing_id)
    return row


def test_new_filing_is_fetched_and_stored_with_its_metadata(db, kit) -> None:
    filing_id, is_new = add(kit, b"one")
    row = filing(filing_id)
    assert is_new and row.status == "fetched" and row.submission == "original"
    assert (row.revision_no, row.period_end, row.period_type, row.basis, row.format) == (
        0, "2026-03-31", "Q", "consolidated", "xbrl",
    )  # fmt: skip
    assert row.filed_on == "2026-05-02" and row.raw_path.endswith(f"{filing_id}.xml")


def test_same_bytes_are_a_duplicate_whatever_the_candidate_says(db, kit) -> None:
    first, _ = add(kit, b"one")
    second, is_new = add(kit, b"one", filing_type="annual_report", basis=Basis.STANDALONE)
    assert (second, is_new) == (first, False)
    assert len(table_rows(tables.filings)) == 1


def test_revision_numbers_follow_the_highest_existing(db, kit) -> None:
    add(kit, b"orig")
    first, _ = add(kit, b"rev-a", submission="revision")
    second, _ = add(kit, b"rev-b", submission="revision")
    assert (filing(first).revision_no, filing(second).revision_no) == (1, 2)


def test_explicit_revision_number_is_kept(db, kit) -> None:
    filing_id, _ = add(kit, b"rev", submission="revision", revision_no=3)
    assert filing(filing_id).revision_no == 3


def test_revision_of_other_period_does_not_share_numbering(db, kit) -> None:
    add(kit, b"a", submission="revision")
    other, _ = add(kit, b"b", submission="revision", period_end=date(2026, 6, 30))
    assert filing(other).revision_no == 1


def test_register_without_isin_is_refused(db, kit) -> None:
    filing_id, path, sha = store_raw(b"x", cand(kit), kit.ALPHA)
    with pytest.raises(registry.RegistryError):
        registry.register(cand(kit, isin=None), RawFile(filing_id, path, sha))


def test_conflicts_quarantine_the_filing_and_open_one_review_item(db, kit) -> None:
    filing_id, _ = add(kit, b"bad", conflicts=[CONFLICT])
    assert filing(filing_id).status == "quarantined"
    (item,) = table_rows(tables.review_queue)
    payload = json.loads(item.payload_json)
    assert item.item_type == "filing_conflict" and item.status == "open"
    assert payload["filing_id"] == filing_id and payload["superseded_by"] == []
    assert payload["conflicts"] == [CONFLICT.as_dict()]


def test_pending_hides_quarantined_filings(db, kit) -> None:
    good, _ = add(kit, b"good", period_end=date(2026, 6, 30))
    bad, _ = add(kit, b"bad", conflicts=[CONFLICT])
    assert [r.filing_id for r in registry.pending(FilingStatus.FETCHED)] == [good]
    assert [r.filing_id for r in registry.pending("quarantined")] == [bad]


def test_item_names_a_revision_that_already_exists(db, kit) -> None:
    revision, _ = add(kit, b"rev", submission="revision")
    add(kit, b"bad", conflicts=[CONFLICT])
    (item,) = table_rows(tables.review_queue)
    assert json.loads(item.payload_json)["superseded_by"] == [revision]


def test_item_is_updated_when_the_revision_arrives_later(db, kit) -> None:
    bad, _ = add(kit, b"bad", conflicts=[CONFLICT])
    revision, _ = add(kit, b"rev", submission="revision")
    (item,) = table_rows(tables.review_queue)
    assert json.loads(item.payload_json)["superseded_by"] == [revision]
    assert filing(bad).status == "quarantined"


def test_revision_of_another_basis_does_not_supersede(db, kit) -> None:
    add(kit, b"bad", conflicts=[CONFLICT])
    add(kit, b"rev", submission="revision", basis=Basis.STANDALONE)
    (item,) = table_rows(tables.review_queue)
    assert json.loads(item.payload_json)["superseded_by"] == []


def test_a_quarantined_revision_does_not_supersede(db, kit) -> None:
    add(kit, b"bad", conflicts=[CONFLICT])
    add(kit, b"badrev", submission="revision", conflicts=[CONFLICT])
    items = table_rows(tables.review_queue)
    assert all(json.loads(i.payload_json)["superseded_by"] == [] for i in items)


def test_release_makes_it_fetched_and_resolves_the_item(db, kit) -> None:
    bad, _ = add(kit, b"bad", conflicts=[CONFLICT])
    registry.release(bad, "checked by hand")
    assert filing(bad).status == "fetched"
    (item,) = table_rows(tables.review_queue)
    assert item.status == "resolved"
    assert json.loads(item.resolution_json)["note"] == "checked by hand"
    assert [r.filing_id for r in registry.pending("fetched")] == [bad]


def test_ignore_keeps_it_quarantined_and_marks_the_item_ignored(db, kit) -> None:
    bad, _ = add(kit, b"bad", conflicts=[CONFLICT])
    registry.ignore(bad)
    assert filing(bad).status == "quarantined"
    (item,) = table_rows(tables.review_queue)
    assert item.status == "ignored"


def test_release_only_applies_to_quarantined_filings(db, kit) -> None:
    good, _ = add(kit, b"good")
    with pytest.raises(registry.RegistryError, match="not quarantined"):
        registry.release(good)
    with pytest.raises(registry.RegistryError, match="unknown"):
        registry.release("FIL-000000000000")


def test_status_transitions(db, kit) -> None:
    filing_id, _ = add(kit, b"x")
    registry.set_status(filing_id, FilingStatus.PARSED)
    registry.set_status(filing_id, FilingStatus.FAILED, "boom")
    row = filing(filing_id)
    assert (row.status, row.error) == ("failed", "boom")
    with pytest.raises(registry.RegistryError):
        registry.set_status("FIL-000000000000", FilingStatus.PARSED)


def test_known_url(db, kit) -> None:
    add(kit, b"x", source_url="https://example.com/a.xml")
    assert registry.known_url("https://example.com/a.xml")
    assert not registry.known_url("https://example.com/b.xml")


def test_entity_lookup(db, kit) -> None:
    lookup = DbEntityLookup()
    for text in (kit.ALPHA, kit.ALPHA.lower(), "alphax", "500001"):
        assert lookup.isin_for(text) == kit.ALPHA
    assert lookup.isin_for("NOPE") is None and lookup.isin_for(kit.ALPHA_TYPO) is None
    assert lookup.symbol_for(kit.BETA) == "BETAX"
    assert lookup.tracked_isins() == sorted([kit.ALPHA, kit.BETA])


def test_unrecognised_items_are_bumped_not_duplicated(db) -> None:
    registry.queue_unrecognised("x.pdf", "bad name")
    registry.queue_unrecognised("x.pdf", "bad name")
    (item,) = table_rows(tables.review_queue)
    assert item.item_type == "inbox_unrecognised" and item.occurrences == 2
