"""A management or narrative question with no period takes the latest period with a transcript.

What management said exists only for the calls held. The latest period with results can be one
whose call is not held (or not filed) yet; asking "what did management say" must not answer from a
period nobody has spoken about. A company with no transcript at all keeps the latest period with
data, and a question that names a period, or asks for a figure, is not changed.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from finrag.core.config import get_settings
from finrag.core.db import tables, transaction
from finrag.core.fiscal import label_to_period
from finrag.metrics.registry import load_metrics
from finrag.normalize.taxonomy import load_taxonomy
from finrag.query.availability import DbAvailability, StubAvailability
from finrag.query.entity_resolver import load_entities
from finrag.query.intent_parser import IntentParser
from finrag.query.intents import load_intents

from .conftest import GABRIEL, HDFC, TODAY

pytestmark = pytest.mark.usefixtures("seeded")
DATA = ["Q4 FY26", "Q1 FY27", "Q2 FY27", "FY26"]  # results exist up to Q2 FY27
CALLS = ["Q4 FY26", "Q1 FY27"]  # the call of Q2 FY27 is not there


def P(label: str):
    return label_to_period(label)


def parse(question: str, transcripts: list[str]):
    taxonomy = load_taxonomy()
    parser = IntentParser(
        entities=load_entities(),
        registry=load_metrics(taxonomy),
        taxonomy=taxonomy,
        intents=load_intents(taxonomy),
        settings=get_settings(),
        availability=StubAvailability(
            [P(x) for x in DATA], transcripts=[P(x) for x in transcripts]
        ),
        llm=None,
    )
    return parser.parse(question, TODAY)


def test_a_management_question_takes_the_latest_period_with_a_transcript() -> None:
    got = parse("What did HDFC management say about margins?", CALLS)
    assert got.periods == [P("Q1 FY27")]  # not Q2 FY27, the latest results
    assert "period_transcript" in got.defaults_applied
    assert "the latest period with an earnings-call transcript is used" in got.interpretation
    assert "the latest with data is shown" not in got.interpretation


def test_a_company_with_no_transcript_keeps_the_latest_period_with_data() -> None:
    got = parse("What did HDFC management say about margins?", [])
    assert got.periods == [P("Q2 FY27")]
    assert "period_transcript" not in got.defaults_applied
    assert "No period was named, so the latest with data is shown." in got.interpretation


def test_a_named_period_is_not_replaced() -> None:
    got = parse("What did HDFC management say about margins in Q4 FY26?", CALLS)
    assert got.periods == [P("Q4 FY26")]
    assert "period" not in got.defaults_applied


def test_a_question_for_a_figure_is_not_a_narrative_question() -> None:
    got = parse("HDFC GNPA latest", CALLS)
    assert got.periods == [P("Q2 FY27")]
    assert "period_transcript" not in got.defaults_applied


def test_a_narrative_question_without_the_word_management_takes_it_too() -> None:
    got = parse("What is HDFC's outlook on deposits?", CALLS)
    assert got.periods == [P("Q1 FY27")]


# --- the stored transcripts ------------------------------------------------------------------


def chunk(kit, chunk_id: str, isin: str, source: str, label: str) -> None:
    period = P(label)
    with transaction() as conn:
        conn.execute(
            sa.insert(tables.chunks).values(
                chunk_id=chunk_id,
                filing_id=kit.filing(isin, label, key=source),
                isin=isin,
                source_type=source,
                section="qa",
                text="x",
                period_start=period.start.isoformat(),
                period_end=period.end.isoformat(),
                period_type=period.type.value,
            )
        )


def test_the_database_names_the_latest_period_with_transcript_passages(kit) -> None:
    chunk(kit, "c1", HDFC, "transcript", "Q4 FY26")
    chunk(kit, "c2", HDFC, "transcript", "Q1 FY27")
    chunk(kit, "c3", HDFC, "results", "Q2 FY27")  # results text is not a call
    chunk(kit, "c4", GABRIEL, "results", "Q1 FY27")
    avail = DbAvailability()
    assert avail.latest_transcript(HDFC) == P("Q1 FY27")
    assert avail.latest_transcript(GABRIEL) is None
