"""M11 data functions: feedback, status, answer and clarification views, the v7 migration."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from finrag.app import data
from finrag.core.config import get_settings
from finrag.core.db import SCHEMA_VERSION, get_engine, init_db, metadata, tables, transaction
from finrag.query.models import Answer, Candidate, Clarification

from .conftest import ALPHA, BETA, GAMMA

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src" / "finrag" / "app"


def make_answer(query_id: str | None = "QRY-1") -> Answer:
    line = "Reading your question as: Alpha — Revenue — Q1 FY27."
    return Answer(
        text=(
            f"{line}\n\n**Figures** — consolidated.\n- [F1] Revenue: 10 crore"
            "\n\n**Sources**\n- [F1] x"
        ),
        numbers_block="**Figures**",
        explanation="",
        citations=[],
        interpretation=line,
        query_id=query_id,
    )


# --- migration -------------------------------------------------------------------------------


def test_version_6_database_gets_the_feedback_table() -> None:
    path = get_settings().paths.db
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa.create_engine(sa.engine.URL.create("sqlite", database=str(path)))
    metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE feedback")
        conn.execute("INSERT INTO schema_version VALUES (6, '2026-10-19T00:00:00+00:00')")
        conn.execute("INSERT INTO entities (isin, name) VALUES ('INE000A00001', 'Alpha Ltd')")
    init_db()
    with transaction() as conn:
        version = conn.execute(sa.select(sa.func.max(tables.schema_version.c.version))).scalar()
        assert version == SCHEMA_VERSION == 7
        assert conn.execute(sa.select(sa.func.count()).select_from(tables.feedback)).scalar() == 0
        assert conn.execute(sa.select(tables.entities.c.name)).scalar() == "Alpha Ltd"
    init_db()  # a second call changes nothing


def test_feedback_columns() -> None:
    init_db()
    columns = {c["name"] for c in sa.inspect(get_engine()).get_columns("feedback")}
    assert {"id", "query_id", "question", "answer", "intent_json", "note", "created_at"} <= columns


# --- feedback --------------------------------------------------------------------------------


def test_a_wrong_answer_writes_question_answer_intent_and_note(logged) -> None:
    settings = get_settings()
    view = data.answer_view(make_answer(), settings)
    feedback_id = data.submit_wrong_answer(
        "how is alpha doing", view, "  revenue is wrong ", settings
    )
    with transaction() as conn:
        row = conn.execute(sa.select(tables.feedback)).one()
        log = conn.execute(sa.select(tables.query_log.c.user_feedback)).scalar()
    assert row.id == feedback_id
    assert (row.query_id, row.question, row.note) == (
        logged,
        "how is alpha doing",
        "revenue is wrong",
    )
    assert json.loads(row.intent_json) == {"metrics": ["revenue"]}
    assert "[F1] Revenue: 10 crore" in row.answer and row.answer.startswith("Reading your question")
    assert json.loads(log)["correct"] is False  # the log row agrees


def test_feedback_without_a_log_row_has_no_intent() -> None:
    init_db()
    view = data.answer_view(make_answer(query_id=None), get_settings())
    data.submit_wrong_answer("q", view, "", get_settings())
    with transaction() as conn:
        row = conn.execute(sa.select(tables.feedback)).one()
    assert row.query_id is None and row.intent_json is None and row.note == ""


def test_an_unknown_query_id_is_an_error_and_writes_nothing(logged) -> None:
    view = data.answer_view(make_answer(query_id="QRY-nope"), get_settings())
    with pytest.raises(KeyError):
        data.submit_wrong_answer("q", view, "n", get_settings())
    with transaction() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(tables.feedback)).scalar() == 0


def test_a_long_note_is_cut_to_the_configured_length(logged) -> None:
    settings = get_settings()
    view = data.answer_view(make_answer(), settings)
    data.submit_wrong_answer("q", view, "x" * (settings.ui.feedback_note_max_chars + 50), settings)
    with transaction() as conn:
        note = conn.execute(sa.select(tables.feedback.c.note)).scalar()
    assert len(note) == settings.ui.feedback_note_max_chars


# --- answer and clarification views ----------------------------------------------------------


def test_the_interpretation_is_above_and_not_repeated_in_the_body() -> None:
    answer = make_answer()
    view = data.answer_view(answer, get_settings())
    assert view.interpretation == answer.interpretation
    assert answer.interpretation not in view.body
    assert view.body.startswith("**Figures**") and "**Sources**" in view.body
    assert f"{view.interpretation}\n\n{view.body}" == answer.text


def test_the_interpretation_can_be_switched_off(isolated_repo) -> None:
    path = isolated_repo / "config" / "settings.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "show_interpretation: true", "show_interpretation: false"
        ),
        encoding="utf-8",
    )
    from finrag.core.config import reset_settings_cache

    reset_settings_cache()
    view = data.answer_view(make_answer(), get_settings())
    assert view.interpretation is None and view.body.startswith("**Figures**")


def test_clarification_options_and_the_reasked_question() -> None:
    result = Clarification(
        question="how is it doing",
        reason="entity",
        message="Which company?",
        candidates=[Candidate("INE1", "Alpha Ltd", 0.9), Candidate("INE2", "Beta Ltd", 0.8)],
    )
    view = data.clarification_view(result)
    assert view.message == "Which company?" and [c.name for c in view.options] == [
        "Alpha Ltd",
        "Beta Ltd",
    ]
    assert data.reask_text(view.question, view.options[1]) == "how is it doing Beta Ltd"


# --- status ----------------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_status_per_company_and_open_review_counts() -> None:
    view = data.status_view()
    by_isin = {c.isin: c for c in view.companies}
    assert [c.name for c in view.companies] == ["Alpha Ltd", "Beta Ltd", "Gamma Ltd"]
    assert (by_isin[ALPHA].latest_period_end, by_isin[ALPHA].latest_transcript_period_end) == (
        "2026-06-30",
        "2026-03-31",  # the newer transcript failed
    )
    assert (by_isin[BETA].latest_period_end, by_isin[BETA].latest_transcript_period_end) == (
        "2026-03-31",
        None,
    )
    assert (by_isin[GAMMA].latest_period_end, by_isin[GAMMA].latest_transcript_period_end) == (
        None,
        None,
    )
    assert view.review_counts == {"corporate_event_proposal": 1, "unmapped_concept": 2}


def test_status_on_an_empty_database() -> None:
    init_db()
    view = data.status_view()
    assert view.companies == [] and view.review_counts == {}


# --- structure rules -------------------------------------------------------------------------


def test_no_app_module_imports_sqlalchemy() -> None:
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+sqlalchemy", text, re.M), path


def test_no_company_names_in_the_new_source_files() -> None:
    wanted = ("gabriel", "hdfc", "netweb")
    files = [*APP.rglob("*.py"), ROOT / "src/finrag/facts/status.py"]
    files.append(ROOT / "src/finrag/query/feedback.py")
    for path in files:
        text = path.read_text(encoding="utf-8").casefold()
        assert not [w for w in wanted if w in text], path


def test_ui_values_come_from_settings() -> None:
    ui = get_settings().ui
    assert (ui.server_address, ui.port) == ("localhost", 8501)
    assert ui.embedder_warmup_text and ui.page_title and ui.feedback_note_max_chars > 0
