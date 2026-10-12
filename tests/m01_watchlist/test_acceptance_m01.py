"""One test per M01 acceptance criterion (docs/modules/M01_watchlist.md)."""

from dataclasses import replace

import pytest

from finrag.core.config import ConfigError
from finrag.core.db import tables
from finrag.watchlist.master import SecuritiesMaster
from finrag.watchlist.resolver import resolve
from finrag.watchlist.schema import parse_watchlist


def test_1_symbol_code_isin_and_name_resolve_to_the_same_isin(master, kit) -> None:
    ids = ["ALPHA", "500001", kit.ALPHA, "Alpha Industries Limited"]
    assert {resolve(i, master).isin for i in ids} == {kit.ALPHA}


def test_2_ambiguous_name_creates_review_item_and_no_entity(run_sync, kit) -> None:
    report = run_sync([{"id": "Gamma Finance"}])
    assert report.added == [] and [n.status for n in report.needs_review] == ["ambiguous"]
    assert kit.rows(tables.entities) == []
    (item,) = kit.rows(tables.review_queue)
    assert (item.item_type, item.status, item.occurrences) == ("entity_ambiguous", "open", 1)
    assert kit.GAMMA1 in item.payload_json and kit.GAMMA2 in item.payload_json


def test_3_second_sync_adds_and_updates_nothing(run_sync, kit) -> None:
    companies = [{"id": "ALPHA"}, {"id": "500002", "sector_type": "bank", "basis": "standalone"}]
    first = run_sync(companies)
    second = run_sync(companies, kit.DAY2)
    assert len(first.added) == 2
    assert (second.added, second.updated) == ([], [])
    assert len(second.unchanged) == 2
    assert kit.entity(kit.ALPHA).updated_on == kit.DAY1.isoformat()


def test_4_removed_company_is_paused_not_deleted(run_sync, kit) -> None:
    run_sync([{"id": "ALPHA"}, {"id": "BETAMOT"}])
    report = run_sync([{"id": "ALPHA"}], kit.DAY2)
    assert report.paused == [kit.BETA]
    assert kit.entity(kit.BETA).status == "paused" and kit.entity(kit.ALPHA).status == "active"
    assert kit.rows(tables.entity_aliases, tables.entity_aliases.c.isin == kit.BETA)  # history kept
    back = run_sync([{"id": "ALPHA"}, {"id": "BETAMOT"}], kit.DAY2)
    assert back.reactivated == [kit.BETA] and kit.entity(kit.BETA).status == "active"


def test_5_symbol_change_closes_old_alias_and_records_rename(run_sync, kit, master) -> None:
    run_sync([{"id": "ALPHA"}])
    renamed = [
        replace(r, nse_symbol="ALPHAIND", name="Alpha Industries Corp Limited")
        if r.isin == kit.ALPHA
        else r
        for r in kit.records()
    ]
    report = run_sync([{"id": kit.ALPHA}], kit.DAY2, SecuritiesMaster(renamed))
    assert report.updated == [kit.ALPHA]
    aliases = kit.rows(
        tables.entity_aliases,
        tables.entity_aliases.c.isin == kit.ALPHA,
        tables.entity_aliases.c.alias_type == "symbol",
    )
    by_alias = {a.alias: a for a in aliases}
    assert by_alias["ALPHA"].valid_to == kit.DAY2.isoformat()
    assert by_alias["ALPHAIND"].valid_from == kit.DAY2.isoformat()
    assert by_alias["ALPHAIND"].valid_to is None
    (event,) = kit.rows(tables.corporate_events)
    assert (event.event_type, event.event_date) == ("rename", kit.DAY2.isoformat())
    assert "ALPHA -> ALPHAIND" in event.details
    assert kit.entity(kit.ALPHA).nse_symbol == "ALPHAIND"


def test_6_invalid_sector_type_fails_with_clear_message() -> None:
    with pytest.raises(ConfigError) as err:
        parse_watchlist({"companies": [{"id": "ALPHA", "sector_type": "airline"}]})
    assert "sector_type 'airline' is not allowed" in str(err.value)
    assert "['general', 'bank', 'nbfc', 'insurance']" in str(err.value)
