"""Shared fixtures: every test runs against a throwaway copy of the repo layout."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from finrag.core import config, db
from finrag.core.logging import reset_logging

REAL_SETTINGS = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"


@pytest.fixture(autouse=True)
def isolated_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point config at a temp repo root holding a copy of settings.yaml; clear FINRAG_* env."""
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    shutil.copy(REAL_SETTINGS, root / "config" / "settings.yaml")
    for name in [n for n in os.environ if n.startswith(config.ENV_PREFIX)]:
        monkeypatch.delenv(name)
    monkeypatch.setattr(config, "REPO_ROOT", root)
    monkeypatch.setattr(config, "SETTINGS_PATH", root / "config" / "settings.yaml")
    config.reset_settings_cache()
    db.reset_engines()
    yield root
    reset_logging()
    db.reset_engines()
    config.reset_settings_cache()
