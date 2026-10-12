import pytest

from finrag.watchlist.master import SecuritiesMaster, SecurityRecord
from finrag.watchlist.resolver import resolve


@pytest.mark.parametrize(
    ("text", "method"),
    [
        ("INE111A01017", "isin"),
        ("ine111a01017", "isin"),
        ("ALPHA", "nse_symbol"),
        (" alpha ", "nse_symbol"),
        ("500001", "bse_code"),
        ("Alpha Industries Limited", "name_exact"),
        ("alpha industries ltd.", "name_exact"),
        ("Alpha Industris", "name_fuzzy"),
    ],
)
def test_each_method_resolves_to_alpha(master, kit, text: str, method: str) -> None:
    result = resolve(text, master)
    assert (result.status, result.isin, result.method) == ("resolved", kit.ALPHA, method)
    assert [c.isin for c in result.candidates] == [kit.ALPHA]


def test_same_name_for_two_companies_is_ambiguous(master, kit) -> None:
    result = resolve("Gamma Finance", master)
    assert result.status == "ambiguous" and result.isin is None
    assert {c.isin for c in result.candidates} == {kit.GAMMA1, kit.GAMMA2}
    assert "Gamma Finance" in result.reason


def test_fuzzy_match_with_two_close_candidates_is_ambiguous() -> None:
    master = SecuritiesMaster(
        [
            SecurityRecord("INE111A01017", "Omega Steel Works Limited", "OMEGA1"),
            SecurityRecord("INE222B01028", "Omega Steel Work Limited", "OMEGA2"),
        ]
    )
    result = resolve("Omega Steel Workz", master)  # scores 94 and 97: both pass, none is picked
    assert result.status == "ambiguous" and result.method == "name_fuzzy"
    assert {c.isin for c in result.candidates} == {"INE111A01017", "INE222B01028"}


def test_fuzzy_threshold_comes_from_settings(master, monkeypatch) -> None:
    from finrag.core.config import get_settings

    settings = get_settings().model_copy(
        update={
            "watchlist": get_settings().watchlist.model_copy(update={"name_match_min_score": 100})
        }
    )
    assert resolve("Alpha Industris", master, settings).status == "not_found"


def test_unknown_text_is_not_found(master) -> None:
    result = resolve("Completely Unknown Enterprises", master)
    assert result.status == "not_found" and result.isin is None and result.reason


def test_valid_isin_missing_from_master_is_not_found(master, kit) -> None:
    result = resolve(kit.make_isin("E999Z0199"), master)
    assert result.status == "not_found" and "not in the securities master" in result.reason


def test_isin_with_bad_check_digit_is_not_found_with_reason(master) -> None:
    result = resolve("INE111A01018", master)  # right shape, wrong check digit
    assert result.status == "not_found"
    assert "check digit" in result.reason


def test_typo_isin_never_falls_through_to_other_methods() -> None:
    master = SecuritiesMaster([SecurityRecord("INE0NT901020", "Netweb Technologies India Limited")])
    assert resolve("INE0NT901020", master).status == "resolved"
    typo = resolve("INEONT901020", master)  # letter O instead of zero
    assert typo.status == "not_found" and "check digit" in typo.reason


def test_blank_identifier_is_not_found(master) -> None:
    assert resolve("  ", master).status == "not_found"
