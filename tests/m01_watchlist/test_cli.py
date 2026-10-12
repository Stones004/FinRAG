from typer.testing import CliRunner

from finrag.cli import app
from finrag.core.config import get_settings

runner = CliRunner()


def _setup(isolated_repo, kit, watchlist: str) -> None:
    settings = get_settings()
    nse = settings.paths.reference / settings.sources.securities_master.nse_file
    kit.write_file(
        nse,
        kit.NSE_HEADER
        + f"\nALPHA,Alpha Industries Limited,EQ,01-JAN-2000,1,1, {kit.ALPHA},1"
        + f"\nGAMMAF,Gamma Finance Limited,EQ,01-JAN-2000,1,1,{kit.GAMMA1},1"
        + f"\nGAMMAF2,Gamma Finance Ltd,EQ,01-JAN-2000,1,1,{kit.GAMMA2},1\n",
    )
    kit.write_file(isolated_repo / "config" / "watchlist.yaml", watchlist)


def test_sync_list_resolve_end_to_end(isolated_repo, kit) -> None:
    _setup(isolated_repo, kit, "companies:\n  - id: ALPHA\n  - id: Gamma Finance\n")

    synced = runner.invoke(app, ["watchlist", "sync"])
    assert synced.exit_code == 0, synced.output
    assert "added 1, updated 0, unchanged 0, needs review 1" in synced.output
    assert "needs review: 'Gamma Finance' (ambiguous)" in synced.output

    again = runner.invoke(app, ["watchlist", "sync"])
    assert "added 0, updated 0, unchanged 1" in again.output

    listed = runner.invoke(app, ["watchlist", "list"])
    assert kit.ALPHA in listed.output and "Alpha Industries Limited" in listed.output

    resolved = runner.invoke(app, ["watchlist", "resolve", "alpha"])
    assert resolved.exit_code == 0 and "resolved via nse_symbol" in resolved.output

    ambiguous = runner.invoke(app, ["watchlist", "resolve", "Gamma Finance"])
    assert ambiguous.exit_code == 1
    assert kit.GAMMA1 in ambiguous.output and kit.GAMMA2 in ambiguous.output


def test_resolve_is_a_dry_run(isolated_repo, kit) -> None:
    _setup(isolated_repo, kit, "companies: []\n")
    assert runner.invoke(app, ["watchlist", "resolve", "ALPHA"]).exit_code == 0
    listed = runner.invoke(app, ["watchlist", "list"])
    assert "No entities yet" in listed.output


def test_invalid_sector_type_is_a_clean_cli_error(isolated_repo, kit) -> None:
    _setup(isolated_repo, kit, "companies:\n  - id: ALPHA\n    sector_type: airline\n")
    result = runner.invoke(app, ["watchlist", "sync"])
    assert result.exit_code == 2
    assert "sector_type 'airline' is not allowed" in result.output


def test_missing_nse_master_is_a_clean_cli_error(isolated_repo, kit) -> None:
    kit.write_file(isolated_repo / "config" / "watchlist.yaml", "companies: []\n")
    result = runner.invoke(app, ["watchlist", "sync"])
    assert result.exit_code == 2 and "NSE securities master not found" in result.output
