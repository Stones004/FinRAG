import logging
from pathlib import Path

import pytest

from finrag.core.config import ConfigError, get_settings
from finrag.watchlist.master import (
    SecuritiesMaster,
    SecurityRecord,
    load_bse,
    load_master,
    load_nse,
    normalize_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Gabriel India Limited", "GABRIEL INDIA"),
        ("Beta Motors Ltd.", "BETA MOTORS"),
        ("  hdfc   bank, LIMITED ", "HDFC BANK"),
        ("A&B (India) Ltd", "A B INDIA"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def _nse_csv(kit) -> str:
    return (
        kit.NSE_HEADER
        + "\nALPHA ,Alpha Industries Limited,EQ,01-JAN-2000,1,1, INE111A01017 ,1"
        + "\nBETAMOT,Beta Motors Ltd.,EQ,01-JAN-2001,2,1,INE222B01028,2\n,No Symbol Ltd,EQ,,,,,\n"
    )


def test_load_nse_strips_header_and_value_whitespace(tmp_path, kit) -> None:
    path = kit.write_file(tmp_path / "nse.csv", _nse_csv(kit))
    records = load_nse(path)
    assert [(r.isin, r.nse_symbol, r.name) for r in records] == [
        ("INE111A01017", "ALPHA", "Alpha Industries Limited"),
        ("INE222B01028", "BETAMOT", "Beta Motors Ltd."),
    ]


def test_load_nse_reports_missing_columns(tmp_path, kit) -> None:
    path = kit.write_file(tmp_path / "nse.csv", "SYMBOL,NAME\nA,B\n")
    with pytest.raises(ConfigError, match="missing expected column"):
        load_nse(path)


def test_load_bse_with_synthetic_file(tmp_path, kit) -> None:
    # Synthetic layout only: the real BSE column names are still a TODO in master.py.
    path = kit.write_file(
        tmp_path / "bse.csv", "Security Code,Security Name,ISIN No\n500001,ALPHA IND,INE111A01017\n"
    )
    assert load_bse(path) == [SecurityRecord("INE111A01017", "ALPHA IND", None, "500001")]


def test_master_merges_nse_and_bse_rows_on_isin() -> None:
    master = SecuritiesMaster(
        [
            SecurityRecord("INE111A01017", "Alpha Industries Limited", "ALPHA"),
            SecurityRecord("INE111A01017", "ALPHA IND", None, "500001"),
        ]
    )
    assert len(master) == 1
    record = master.by_isin("ine111a01017")
    assert record == SecurityRecord("INE111A01017", "Alpha Industries Limited", "ALPHA", "500001")
    assert master.by_symbol("alpha") is record and master.by_code("500001") is record
    assert master.by_norm_name("alpha industries ltd") == [record]


def test_load_master_without_bse_file_warns_but_works(isolated_repo, kit, caplog) -> None:
    settings = get_settings()
    kit.write_file(
        settings.paths.reference / settings.sources.securities_master.nse_file, _nse_csv(kit)
    )
    logger = logging.getLogger("finrag.watchlist")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="finrag.watchlist"):
            master = load_master(settings)
    finally:
        logger.removeHandler(caplog.handler)
    assert len(master) == 2
    assert any("BSE securities master not found" in m for m in caplog.messages)


def test_load_master_without_nse_file_is_an_error() -> None:
    with pytest.raises(ConfigError, match="NSE securities master not found"):
        load_master()


def test_real_nse_file_layout_loads_when_present() -> None:
    real = Path(__file__).resolve().parents[2] / "data" / "reference" / "nse_equity_list.csv"
    if not real.is_file():
        pytest.skip("real NSE equity list not present")
    records = {r.nse_symbol: r for r in load_nse(real)}
    assert records["HDFCBANK"].isin == "INE040A01034"
    assert records["NETWEB"].isin == "INE0NT901020"
