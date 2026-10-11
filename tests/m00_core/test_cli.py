import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from finrag.cli import app
from finrag.core.db import SCHEMA_VERSION, get_engine

runner = CliRunner()


def test_init_leaves_existing_files_untouched(isolated_repo: Path) -> None:
    inbox = isolated_repo / "data" / "inbox"
    inbox.mkdir(parents=True)
    keep = inbox / "filing.xml"
    keep.write_bytes(b"<xbrl>original</xbrl>")
    nested = isolated_repo / "data" / "raw" / "INE0" / "a.pdf"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"%PDF original")
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (keep, nested)}

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before
    created = [line for line in result.output.splitlines() if line.startswith("created")]
    assert not any(line.endswith(("inbox", "raw")) for line in created)
    assert (isolated_repo / "data" / "indexes").is_dir()


def test_init_is_repeatable() -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0 and "created" not in second.output
    with get_engine().connect() as conn:
        assert conn.execute(sa.text("select count(*) from schema_version")).scalar() == 1
        assert (
            conn.execute(sa.text("select max(version) from schema_version")).scalar()
            == SCHEMA_VERSION
        )


def test_config_show_masks_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINRAG_ALERTS__EMAIL__PASSWORD", "s3cret")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "s3cret" not in result.output
    assert json.loads(result.output)["alerts"]["email"]["password"] == "***"


def test_bad_env_override_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINRAG_APP__NAME", "x")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2 and "FINRAG_APP__NAME" in result.output


def test_status_placeholder() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0 and "No status" in result.output
