from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from finrag.core import config
from finrag.core.config import ConfigError, get_settings, load_registry, load_settings


def _edit(root: Path, mutate: Callable[[dict[str, Any]], object]) -> None:
    path = root / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_real_settings_load_and_paths_resolve_against_root(isolated_repo: Path) -> None:
    settings = get_settings()
    assert settings.paths.db == isolated_repo / "data" / "finrag.db"
    assert settings.fiscal.fy_start_month == 4
    assert settings.llm.model == ""  # blank [VERIFY] values are valid at load time


def test_settings_are_cached_until_reset() -> None:
    first = get_settings()
    assert get_settings() is first
    config.reset_settings_cache()
    assert get_settings() is not first


def test_unknown_key_is_rejected(isolated_repo: Path) -> None:
    _edit(isolated_repo, lambda d: d["app"].update(typo=1))
    with pytest.raises(ConfigError, match=r"app\.typo"):
        load_settings()


def test_missing_key_is_named(isolated_repo: Path) -> None:
    _edit(isolated_repo, lambda d: d["history"].pop("years_back"))
    with pytest.raises(ConfigError, match=r"history\.years_back"):
        load_settings()


def test_cross_field_check_names_the_key(isolated_repo: Path) -> None:
    _edit(isolated_repo, lambda d: d["defaults"].update(display_unit="furlong"))
    with pytest.raises(ConfigError, match=r"defaults\.display_unit"):
        load_settings()


def test_missing_settings_file(isolated_repo: Path) -> None:
    (isolated_repo / "config" / "settings.yaml").unlink()
    with pytest.raises(ConfigError, match="not found"):
        load_settings()


def test_secret_env_override_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINRAG_ALERTS__EMAIL__PASSWORD", "s3cret")
    assert get_settings().alerts.email.password == "s3cret"


def test_non_secret_env_override_is_an_error_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINRAG_APP__LOG_LEVEL", "DEBUG")
    with pytest.raises(ConfigError, match="FINRAG_APP__LOG_LEVEL"):
        get_settings()


def test_masked_settings_hides_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINRAG_ALERTS__EMAIL__PASSWORD", "s3cret")
    dumped = config.masked_settings()
    assert dumped["alerts"]["email"]["password"] == config.MASK
    assert "s3cret" not in str(dumped)


def test_load_registry(isolated_repo: Path) -> None:
    (isolated_repo / "config" / "taxonomy.yaml").write_text("version: 1\n", encoding="utf-8")
    assert load_registry("taxonomy") == {"version": 1}


def test_load_registry_rejects_unknown_name_and_missing_file() -> None:
    with pytest.raises(ConfigError, match="unknown registry"):
        load_registry("prompts")
    with pytest.raises(ConfigError, match="not found"):
        load_registry("intents")
