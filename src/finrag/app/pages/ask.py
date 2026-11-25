"""Ask page: question box, interpretation line, clarification options, answer, feedback."""

from __future__ import annotations

import streamlit as st

from finrag.app import data, runtime
from finrag.query.models import Answer, Clarification

QUESTION_KEY = "ask_question"
RESULT_KEY = "ask_result"
SENT_KEY = "ask_feedback_sent"


def _ask(question: str) -> None:
    """Ask the service and keep the result for the next rerun."""
    st.session_state[QUESTION_KEY] = question
    st.session_state[RESULT_KEY] = runtime.get_service().ask(question)
    st.session_state[SENT_KEY] = False


def _escape(text: str) -> str:
    """Markdown with '$' kept literal (Streamlit would read $...$ as maths)."""
    return text.replace("$", r"\$")


def _clarification(result: Clarification) -> None:
    view = data.clarification_view(result)
    st.warning(view.message)
    for n, candidate in enumerate(view.options):
        if st.button(candidate.name, key=f"candidate_{n}"):
            _ask(data.reask_text(view.question, candidate))
            st.rerun()


def _answer(answer: Answer, question: str) -> None:
    settings = runtime.app_settings()
    view = data.answer_view(answer, settings)
    if view.interpretation:
        st.info(_escape(view.interpretation))
    st.markdown(_escape(view.body))
    st.divider()
    if st.session_state.get(SENT_KEY):
        st.success("Feedback saved.")
        return
    with st.form("wrong_answer"):
        note = st.text_area("What is wrong with this answer?", key="ask_note")
        if st.form_submit_button("This answer is wrong"):
            data.submit_wrong_answer(question, view, note, settings, runtime.get_app_engine())
            st.session_state[SENT_KEY] = True
            st.rerun()


def render() -> None:
    """Draw the page."""
    st.header("Ask")
    with st.form("question_form"):
        text = st.text_input("Question", value=st.session_state.get(QUESTION_KEY, ""))
        submitted = st.form_submit_button("Ask")
    if submitted and text.strip():
        with st.spinner("Working on it"):
            _ask(text.strip())
    result = st.session_state.get(RESULT_KEY)
    if isinstance(result, Clarification):
        _clarification(result)
    elif isinstance(result, Answer):
        _answer(result, st.session_state.get(QUESTION_KEY, ""))
