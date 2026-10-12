import pytest

from finrag.core.config import ConfigError
from finrag.watchlist.schema import load_watchlist, parse_watchlist


def test_defaults_and_int_ids_become_text() -> None:
    wl = parse_watchlist({"companies": [{"id": 500999, "sector_type": "bank"}, {"id": " ABC "}]})
    assert [c.id for c in wl.companies] == ["500999", "ABC"]
    assert wl.companies[0].sector_type == "bank" and wl.companies[1].sector_type is None
    assert wl.companies[1].status == "active" and wl.companies[1].basis is None


def test_empty_companies_is_allowed() -> None:
    assert parse_watchlist({"companies": None}).companies == []
    assert parse_watchlist({}).companies == []


def test_invalid_sector_type_names_entry_and_allowed_values() -> None:
    with pytest.raises(ConfigError) as err:
        parse_watchlist({"companies": [{"id": "A"}, {"id": "B", "sector_type": "airline"}]})
    message = str(err.value)
    assert "companies.1.sector_type" in message and "'airline'" in message
    assert "general" in message and "bank" in message


@pytest.mark.parametrize(
    "entry",
    [{"id": "A", "basis": "group"}, {"id": "A", "status": "gone"}, {"id": ""}, {"id": "A", "x": 1}],
)
def test_other_invalid_entries_are_rejected(entry: dict) -> None:
    with pytest.raises(ConfigError):
        parse_watchlist({"companies": [entry]})


def test_load_watchlist_reads_the_configured_file(isolated_repo, kit) -> None:
    text = "companies:\n  - id: ALPHA\n    basis: standalone\n  - id: 500002\n"
    kit.write_file(isolated_repo / "config" / "watchlist.yaml", text)
    wl = load_watchlist()
    assert [(c.id, c.basis) for c in wl.companies] == [("ALPHA", "standalone"), ("500002", None)]
