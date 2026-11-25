"""M11 app smoke tests with Streamlit's AppTest: a fake service, no model, no network."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
import streamlit as st
from streamlit.testing.v1 import AppTest

from finrag.app import runtime
from finrag.core.db import init_db, tables, transaction
from finrag.query.models import Answer, Candidate, Clarification

APP_FILE = str(Path(__file__).resolve().parents[2] / "src" / "finrag" / "app" / "main.py")

LINE = "Reading your question as: Alpha — Revenue — Q1 FY27."
TEXT = f"{LINE}\n\n**Figures** — consolidated.\n- [F1] Revenue: 10 crore\n\n**Sources**\n- [F1] x"


class FakeService:
    """Answers 'which' with a clarification and anything else with a fixed answer."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def ask(self, question: str):
        self.asked.append(question)
        if question.startswith("which") and "Ltd" not in question:
            return Clarification(
                question=question,
                reason="entity",
                message="Which company?",
                candidates=[
                    Candidate("INE1", "Alpha Ltd", 0.9),
                    Candidate("INE2", "Beta Ltd", 0.8),
                ],
            )
        return Answer(
            text=TEXT,
            numbers_block="",
            explanation="",
            citations=[],
            interpretation=LINE,
            query_id=None,
        )


@pytest.fixture(autouse=True)
def fresh_caches():
    """st.cache_resource outlives a test; the cached engine would point at an old temp database."""
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.fixture
def service(monkeypatch) -> FakeService:
    fake = FakeService()
    init_db()
    monkeypatch.setattr(runtime, "get_service", lambda: fake)
    return fake


def ask_in(at: AppTest, question: str) -> AppTest:
    """Type the question and press the form's Ask button."""
    at.text_input[0].set_value(question)
    next(b for b in at.button if b.label == "Ask").click()
    return at.run()


def run_app() -> AppTest:
    return AppTest.from_file(APP_FILE, default_timeout=30).run()


def test_the_app_starts_on_the_ask_page(service) -> None:
    at = run_app()
    assert not at.exception
    assert [h.value for h in at.header] == ["Ask"]
    assert at.text_input[0].label == "Question"


def test_a_question_shows_the_interpretation_above_the_answer_and_a_feedback_button(
    service,
) -> None:
    at = run_app()
    ask_in(at, "how is alpha doing")
    assert service.asked == ["how is alpha doing"]
    assert [i.value for i in at.info] == [LINE]
    shown = " ".join(m.value for m in at.markdown)
    assert "[F1] Revenue: 10 crore" in shown and "**Sources**" in shown
    assert shown.count(LINE) == 0  # the line is in the info box only
    assert "This answer is wrong" in [b.label for b in at.button]


def test_a_clarification_shows_options_that_ask_again_with_the_name_appended(service) -> None:
    at = run_app()
    ask_in(at, "which one")
    assert [w.value for w in at.warning] == ["Which company?"]
    labels = [b.label for b in at.button]
    assert "Alpha Ltd" in labels and "Beta Ltd" in labels
    next(b for b in at.button if b.label == "Beta Ltd").click().run()
    assert service.asked[-1] == "which one Beta Ltd"
    assert [i.value for i in at.info] == [LINE]


def test_this_answer_is_wrong_writes_a_feedback_row(service) -> None:
    at = run_app()
    ask_in(at, "how is alpha doing")
    at.text_area[0].set_value("revenue is off")
    next(b for b in at.button if b.label == "This answer is wrong").click().run()
    with transaction() as conn:
        row = conn.execute(sa.select(tables.feedback)).one()
    assert (row.question, row.note) == ("how is alpha doing", "revenue is off")
    assert LINE in row.answer and "[F1] Revenue: 10 crore" in row.answer
    assert [s.value for s in at.success] == ["Feedback saved."]


@pytest.mark.usefixtures("seeded")
def test_the_status_page_lists_companies_and_review_counts(service) -> None:
    at = run_app()
    at.sidebar.radio[0].set_value("Status").run()
    assert not at.exception
    assert [h.value for h in at.header] == ["Status"]
    companies, review = at.dataframe[0].value, at.dataframe[1].value
    assert list(companies["Company"]) == ["Alpha Ltd", "Beta Ltd", "Gamma Ltd"]
    assert list(companies["Latest period with data"])[:2] == ["2026-06-30", "2026-03-31"]
    assert dict(zip(review["Type"], review["Open"], strict=True)) == {
        "corporate_event_proposal": 1,
        "unmapped_concept": 2,
    }


# --- cached resources ------------------------------------------------------------------------


def test_the_embedding_model_and_connection_are_built_once(monkeypatch) -> None:
    built: list[object] = []

    class CountingEmbedder:
        def __init__(self, settings) -> None:
            built.append(self)

        def embed_query(self, text: str):
            return [0.0]

    runtime.get_embedder.clear()
    runtime.get_app_engine.clear()
    monkeypatch.setattr(runtime, "SentenceTransformerEmbedder", CountingEmbedder)
    at = AppTest.from_function(_use_runtime, default_timeout=30)
    at.run()
    at.run()  # a second script run is a second question
    assert len(built) == 1
    assert at.session_state["same_engine"] is True
    runtime.get_embedder.clear()
    runtime.get_app_engine.clear()


def _use_runtime() -> None:
    import streamlit as st

    from finrag.app import runtime

    first = runtime.get_embedder()
    second = runtime.get_embedder()
    st.session_state["same_embedder"] = first is second
    st.session_state["same_engine"] = runtime.get_app_engine() is runtime.get_app_engine()


def test_finrag_ui_runs_streamlit_on_the_configured_address_and_port(monkeypatch) -> None:
    import subprocess

    from typer.testing import CliRunner

    from finrag.cli import app

    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "call", lambda command: calls.append(command) or 0)
    result = CliRunner().invoke(app, ["ui"])
    assert result.exit_code == 0 and "http://localhost:8501" in result.output
    command = calls[0]
    assert command[1:4] == ["-m", "streamlit", "run"] and command[4].endswith("main.py")
    assert command[command.index("--server.address") + 1] == "localhost"
    assert command[command.index("--server.port") + 1] == "8501"
