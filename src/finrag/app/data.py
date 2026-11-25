"""What the pages show, as plain structures (no Streamlit): tested without a browser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from finrag.core.config import Settings
from finrag.facts.status import CompanyStatus, company_status, open_review_counts
from finrag.query.feedback import record_wrong_answer
from finrag.query.models import Answer, Candidate, Clarification


@dataclass(frozen=True)
class AnswerView:
    """An answer split for the page: interpretation line above, the rest as `finrag ask` prints."""

    interpretation: str | None
    body: str
    query_id: str | None


@dataclass(frozen=True)
class ClarificationView:
    """A question that needs a choice: the message and one option per candidate."""

    message: str
    options: list[Candidate]
    question: str


@dataclass(frozen=True)
class StatusView:
    """The status page: one row per company and the open review items by type."""

    companies: list[CompanyStatus]
    review_counts: dict[str, int]


def answer_view(answer: Answer, settings: Settings) -> AnswerView:
    """The interpretation line goes above the answer when `ui.show_interpretation`.

    `answer.text` is what `finrag ask` prints: it starts with the interpretation line, which is
    taken off the body here so it is not shown twice.
    """
    body = answer.text
    if answer.interpretation and body.startswith(answer.interpretation):
        body = body[len(answer.interpretation) :].lstrip("\n")
    line = answer.interpretation if settings.ui.show_interpretation else None
    return AnswerView(line, body, answer.query_id)


def clarification_view(result: Clarification) -> ClarificationView:
    """A Clarification -> its message and options."""
    return ClarificationView(result.message, list(result.candidates), result.question)


def reask_text(question: str, candidate: Candidate) -> str:
    """The question asked again with the chosen candidate's name appended.

    Limitation: the name is text, not an id, so it can still resolve to something else; a service
    entry point that answers with the chosen id would remove that (see docs/PROGRESS.md).
    """
    return f"{question} {candidate.name}"


def submit_wrong_answer(
    question: str, view: AnswerView, note: str, settings: Settings, engine: Any = None
) -> int:
    """Write the 'this answer is wrong' feedback row; the note is cut to the configured length."""
    shown = view.body if view.interpretation is None else f"{view.interpretation}\n\n{view.body}"
    return record_wrong_answer(
        question,
        shown,
        note.strip()[: settings.ui.feedback_note_max_chars],
        view.query_id,
        engine,
    )


def status_view(engine: Any = None) -> StatusView:
    """Data for the status page."""
    return StatusView(company_status(engine), open_review_counts(engine))
