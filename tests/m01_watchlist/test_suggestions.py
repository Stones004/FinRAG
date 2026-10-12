"""Suggestions for names that match nothing: shown, queued, never auto-picked."""

import json

from finrag.core.config import get_settings
from finrag.core.db import tables
from finrag.watchlist.master import SecuritiesMaster, SecurityRecord
from finrag.watchlist.resolver import resolve

HDFC = SecurityRecord("INE040A01034", "HDFC Bank Limited", "HDFCBANK")
HDFCLIFE = SecurityRecord("INE795G01014", "HDFC Life Insurance Company Limited", "HDFCLIFE")
HDFCAMC = SecurityRecord("INE127D01025", "HDFC Asset Management Company Limited", "HDFCAMC")
TATAM = SecurityRecord("INE155A01022", "Tata Motors Limited", "TATAMOTORS")
TATAS = SecurityRecord("INE081A01020", "Tata Steel Limited", "TATASTEEL")
OTHER = SecurityRecord("INE111A01017", "Alpha Industries Limited", "ALPHA")


def big_master(extra: int = 0) -> SecuritiesMaster:
    records = [HDFC, HDFCLIFE, HDFCAMC, TATAM, TATAS, OTHER]
    for i in range(extra):
        records.append(SecurityRecord(f"INE9{i:07d}", f"Tata Extra {i} Limited", f"TATAX{i}"))
    return SecuritiesMaster(records)


def test_hdfc_returns_suggestions_instead_of_bare_not_found() -> None:
    result = resolve("HDFC", big_master())
    assert result.status == "not_found" and result.isin is None and not result.candidates
    assert {r.isin for r in result.suggestions} == {HDFC.isin, HDFCLIFE.isin, HDFCAMC.isin}


def test_tata_suggests_tata_companies_only() -> None:
    result = resolve("Tata", big_master())
    assert result.status == "not_found"
    assert {r.isin for r in result.suggestions} == {TATAM.isin, TATAS.isin}


def test_symbol_prefix_matches_come_first() -> None:
    master = SecuritiesMaster(
        [
            SecurityRecord("INE111A01017", "Zeta Holdings Limited", "ACME1"),
            SecurityRecord("INE222B01028", "Acme Zeta Corp Limited", "XYZ"),
        ]
    )
    result = resolve("Acme", master)
    assert [r.nse_symbol for r in result.suggestions] == ["ACME1", "XYZ"]


def test_at_most_suggest_max_suggestions() -> None:
    result = resolve("Tata", big_master(extra=20))
    assert result.status == "not_found"
    assert len(result.suggestions) == get_settings().watchlist.suggest_max == 5


def test_suggest_max_and_floor_come_from_settings() -> None:
    settings = get_settings()
    tight = settings.model_copy(
        update={
            "watchlist": settings.watchlist.model_copy(
                update={"suggest_max": 1, "suggest_min_score": 100}
            )
        }
    )
    assert len(resolve("Tata", big_master(), tight).suggestions) == 1
    assert resolve("Qwerty Unrelated", big_master(), tight).suggestions == ()


def test_no_suggestions_when_nothing_is_close(master) -> None:
    result = resolve("Completely Unknown Enterprises", master)
    assert result.status == "not_found" and result.suggestions == ()


def test_resolved_and_ambiguous_carry_no_suggestions(master) -> None:
    assert resolve("ALPHA", master).suggestions == ()
    assert resolve("Gamma Finance", master).suggestions == ()


def test_bad_isin_gets_no_suggestions(master) -> None:
    assert resolve("INE111A01018", master).suggestions == ()


def test_sync_attaches_suggestions_to_the_review_item(run_sync, kit) -> None:
    report = run_sync([{"id": "Alph"}], use_master=big_master())
    (item,) = report.needs_review
    assert item.status == "not_found" and [r.isin for r in item.suggestions] == [OTHER.isin]
    (row,) = kit.rows(tables.review_queue)
    payload = json.loads(row.payload_json)
    assert payload["suggestions"] == [
        {"isin": OTHER.isin, "name": OTHER.name, "symbol": OTHER.nse_symbol}
    ]
    assert payload["candidates"] == []


def test_cli_resolve_prints_did_you_mean(monkeypatch) -> None:
    from typer.testing import CliRunner

    from finrag.cli import app

    monkeypatch.setattr("finrag.watchlist.master.load_master", lambda *a, **k: big_master())
    result = CliRunner().invoke(app, ["watchlist", "resolve", "HDFC"])
    assert result.exit_code == 1
    assert "did you mean" in result.output and "HDFCBANK" in result.output
