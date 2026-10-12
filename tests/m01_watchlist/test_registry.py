import json

from finrag.core.db import tables


def test_new_entity_takes_values_from_master_yaml_and_defaults(run_sync, kit) -> None:
    report = run_sync(
        [{"id": "ALPHA"}, {"id": "500002", "sector_type": "bank", "basis": "standalone"}]
    )
    assert report.added == [kit.ALPHA, kit.BETA] == report.newly_added
    alpha, beta = kit.entity(kit.ALPHA), kit.entity(kit.BETA)
    assert (alpha.name, alpha.nse_symbol, alpha.bse_code) == (
        "Alpha Industries Limited", "ALPHA", "500001",
    )  # fmt: skip
    assert (alpha.sector_type, alpha.default_basis, alpha.status) == (
        "general", "consolidated", "active",
    )  # fmt: skip
    assert (beta.sector_type, beta.default_basis) == ("bank", "standalone")
    assert alpha.added_on == alpha.updated_on == kit.DAY1.isoformat()


def test_aliases_written_for_symbol_code_name_and_user_input(run_sync, kit) -> None:
    run_sync([{"id": "Alpha Industries Ltd"}])
    pairs = {
        (a.alias_type, a.alias)
        for a in kit.rows(tables.entity_aliases, tables.entity_aliases.c.isin == kit.ALPHA)
    }
    assert pairs == {
        ("symbol", "ALPHA"),
        ("code", "500001"),
        ("name", "Alpha Industries Limited"),
        ("user", "Alpha Industries Ltd"),
    }


def test_user_alias_not_duplicated_when_input_equals_symbol(run_sync, kit) -> None:
    run_sync([{"id": "alpha"}])
    types = [a.alias_type for a in kit.rows(tables.entity_aliases)]
    assert "user" not in types


def test_yaml_override_changes_are_applied_once(run_sync, kit) -> None:
    run_sync([{"id": "ALPHA"}])
    report = run_sync([{"id": "ALPHA", "sector_type": "nbfc", "basis": "standalone"}], kit.DAY2)
    assert report.updated == [kit.ALPHA]
    row = kit.entity(kit.ALPHA)
    assert (row.sector_type, row.default_basis, row.updated_on) == (
        "nbfc", "standalone", kit.DAY2.isoformat(),
    )  # fmt: skip
    assert kit.rows(tables.corporate_events) == []  # not a rename


def test_paused_in_yaml_then_active_again_is_reported_as_reactivated(run_sync, kit) -> None:
    first = run_sync([{"id": "ALPHA", "status": "paused"}])
    assert first.added == [kit.ALPHA] and first.paused == [kit.ALPHA]
    assert kit.entity(kit.ALPHA).status == "paused"
    second = run_sync([{"id": "ALPHA", "status": "active"}], kit.DAY2)
    assert second.reactivated == [kit.ALPHA] and second.updated == [kit.ALPHA]
    assert kit.entity(kit.ALPHA).status == "active"


def test_unresolved_id_of_a_tracked_company_does_not_pause_it(run_sync, kit) -> None:
    run_sync([{"id": "ALPHA"}])
    # The master no longer knows ALPHA, but the entity is still tracked under that alias.
    from finrag.watchlist.master import SecuritiesMaster

    empty = SecuritiesMaster([r for r in kit.records() if r.isin != kit.ALPHA])
    report = run_sync([{"id": "ALPHA"}], kit.DAY2, empty)
    assert [n.status for n in report.needs_review] == ["not_found"]
    assert report.paused == [] and kit.entity(kit.ALPHA).status == "active"


def test_review_item_occurrences_increment_instead_of_duplicating(run_sync, kit) -> None:
    for day in (kit.DAY1, kit.DAY2, kit.DAY2):
        run_sync([{"id": "Nowhere Corp"}, {"id": "Gamma Finance"}], day)
    items = kit.rows(tables.review_queue)
    assert sorted(i.occurrences for i in items) == [3, 3]
    assert {json.loads(i.payload_json)["identifier"] for i in items} == {
        "Nowhere Corp", "Gamma Finance",
    }  # fmt: skip


def test_check_digit_failure_goes_to_review_with_reason(run_sync, kit) -> None:
    report = run_sync([{"id": "INE111A01018"}])
    assert report.added == [] and kit.rows(tables.entities) == []
    (item,) = kit.rows(tables.review_queue)
    payload = json.loads(item.payload_json)
    assert payload["status"] == "not_found" and "check digit" in payload["reason"]


def test_two_entries_for_one_company_are_flagged_not_duplicated(run_sync, kit) -> None:
    report = run_sync([{"id": "ALPHA"}, {"id": "500001"}])
    assert report.added == [kit.ALPHA]
    assert [n.status for n in report.needs_review] == ["duplicate"]
    assert len(kit.rows(tables.entities)) == 1


def test_bse_code_added_later_creates_alias_without_rename(run_sync, kit) -> None:
    from dataclasses import replace

    from finrag.watchlist.master import SecuritiesMaster

    bare = [replace(r, bse_code=None) if r.isin == kit.ALPHA else r for r in kit.records()]
    run_sync([{"id": "ALPHA"}], kit.DAY1, SecuritiesMaster(bare))
    report = run_sync([{"id": "ALPHA"}], kit.DAY2)
    assert report.updated == [kit.ALPHA]
    assert kit.entity(kit.ALPHA).bse_code == "500001"
    assert kit.rows(tables.corporate_events) == []
