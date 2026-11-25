"""'This answer is wrong': one feedback row with the question, answer, intent and a note."""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.db import tables, transaction
from finrag.core.types import utc_now_iso


def record_wrong_answer(
    question: str,
    answer: str,
    note: str = "",
    query_id: str | None = None,
    engine: Engine | None = None,
) -> int:
    """Store the feedback row (intent from the query_log row of `query_id`); returns its id.

    The query_log row is marked as wrong too, so the log and the feedback table agree.
    """
    log = tables.query_log
    with transaction(engine) as conn:
        intent_json = None
        if query_id is not None:
            row = conn.execute(
                sa.select(log.c.intent_json).where(log.c.query_id == query_id)
            ).first()
            if row is None:
                raise KeyError(f"no query_log row {query_id}")
            intent_json = row.intent_json
            marked = json.dumps({"correct": False, "note": note, "at": utc_now_iso()})
            conn.execute(
                sa.update(log).where(log.c.query_id == query_id).values(user_feedback=marked)
            )
        result = conn.execute(
            sa.insert(tables.feedback).values(
                query_id=query_id,
                question=question,
                answer=answer,
                intent_json=intent_json,
                note=note,
                created_at=utc_now_iso(),
            )
        )
        return int(result.inserted_primary_key[0])
