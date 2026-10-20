"""ask(question) -> Answer or Clarification, one query_log row per call; feedback on an answer."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.ids import PREFIX_QUERY, new_id
from finrag.core.logging import get_logger
from finrag.core.types import utc_now_iso
from finrag.llm.base import LLMClient
from finrag.llm.openai_compat import OpenAICompatClient
from finrag.metrics.registry import MetricRegistry, load_metrics
from finrag.narrative.embedder import Embedder, SentenceTransformerEmbedder
from finrag.normalize.taxonomy import Taxonomy, load_taxonomy
from finrag.query.answerer import (
    LLM_DRAFT,
    NO_TRANSCRIPT_NOTE,
    NO_USABLE_PASSAGE_NOTE,
    Answerer,
    Draft,
)
from finrag.query.availability import Availability, DbAvailability
from finrag.query.claims import ClaimRules
from finrag.query.formatter import Formatter
from finrag.query.intent_parser import IntentParser, build_parser
from finrag.query.models import Answer, Citation, Clarification, Evidence, Intent, Plan
from finrag.query.numbers_block import NumbersBlock, NumbersBlockBuilder
from finrag.query.planner import Planner
from finrag.query.postcheck import (
    NO_EXPLANATION,
    NO_GUIDANCE,
    PostcheckResult,
    build_citations,
    postcheck,
)
from finrag.query.retriever import Retriever

log = get_logger("query.service")

LLM_DOWN_NOTE = "The language model server is not reachable, so only the figures are shown."


class QueryService:
    """The whole path from a question to a checked, cited, logged answer."""

    def __init__(
        self,
        parser: IntentParser,
        planner: Planner,
        retriever: Retriever,
        answerer: Answerer,
        builder: NumbersBlockBuilder,
        settings: Settings,
        llm: LLMClient | None = None,
        engine: Engine | None = None,
    ) -> None:
        self.parser = parser
        self.planner = planner
        self.retriever = retriever
        self.answerer = answerer
        self.builder = builder
        self.settings = settings
        self.llm = llm
        self.engine = engine

    # -- public ------------------------------------------------------------------------------

    def explain_plan(
        self, question: str, today: date | None = None
    ) -> tuple[Intent, Plan] | Clarification:
        """Question -> intent and plan, with no retrieval, no LLM call and no log row."""
        parsed = self._parse(question, today)[0]
        if isinstance(parsed, Clarification):
            return parsed
        return parsed, self.planner.plan(parsed)

    def ask(self, question: str, today: date | None = None) -> Answer | Clarification:
        """Question -> checked Answer (or a Clarification); always writes one query_log row."""
        parsed, llm_down = self._parse(question, today)
        if isinstance(parsed, Clarification):
            parsed.query_id = self._log(
                question, parsed.to_json(), {}, [], "", "", parsed.message, [], {}
            )
            return parsed
        intent = parsed
        plan = self.planner.plan(intent)
        evidence = self.retriever.retrieve(plan)
        if intent.exploratory and not self._close_enough(evidence):
            asked = self.parser.no_match_clarification(intent)
            asked.query_id = self._log(
                question,
                intent.to_json(),
                plan.to_json(),
                self._retrieved_ids(evidence),
                "",
                "",
                asked.message,
                [],
                {"top_scores": evidence.top_scores},
            )
            return asked
        block = self.builder.build(intent, plan, evidence)
        draft = self.answerer.draft(intent, plan, evidence, block)
        check = self._check(draft, intent, plan, evidence, block)
        answer = self._compose(intent, evidence, block, draft, check, llm_down)
        response = draft.response
        answer.query_id = self._log(
            question,
            intent.to_json(),
            plan.to_json(),
            self._retrieved_ids(evidence),
            self.settings.llm.prompt_version if response else "",
            response.model if response else "",
            answer.text,
            [c.id for c in answer.citations],
            {
                "kept": [c.to_json() for c in check.kept],
                "dropped": check.dropped,
                "status": draft.status,
            },
        )
        return answer

    def feedback(self, query_id: str, correct: bool, note: str = "") -> None:
        """Record whether an answer was right (user_feedback of its query_log row)."""
        q = tables.query_log
        value = json.dumps({"correct": correct, "note": note, "at": utc_now_iso()})
        with transaction(self.engine) as conn:
            done = conn.execute(
                sa.update(q).where(q.c.query_id == query_id).values(user_feedback=value)
            )
            if done.rowcount == 0:
                raise KeyError(f"no query_log row {query_id}")

    # -- steps -------------------------------------------------------------------------------

    def _parse(self, question: str, today: date | None) -> tuple[Intent | Clarification, bool]:
        """Parse with the LLM only if its server answers; returns (result, server_was_down)."""
        usable = self.llm if self.llm is not None and self.llm.ping() else None
        down = self.llm is not None and usable is None
        self.parser.llm = usable
        self.answerer.llm = usable
        return self.parser.parse(question, today), down

    def _close_enough(self, evidence: Evidence) -> bool:
        """The best hit of an exploratory search must reach the configured similarity."""
        floor = self.settings.query.retrieval.explore_min_dense_score
        return bool(evidence.chunks) and evidence.top_scores.get("dense", 0.0) >= floor

    def _rules(self, intent: Intent, plan: Plan) -> ClaimRules:
        """The registries and the question -> what a claim is checked against."""
        words = self.answerer.intents
        periods = list(dict.fromkeys(r.period for r in plan.requests))
        return ClaimRules(
            periods=tuple(periods),
            bind_period=not intent.forward_looking,  # guidance is about periods not filed yet
            causal_words=tuple(words.causal_words),
            hedge_words=tuple(words.hedge_words),
            management_terms=tuple(words.speaker_terms["management"]),
            analyst_terms=tuple(words.speaker_terms["analyst"]),
            management_roles=frozenset(r.casefold() for r in words.management_speaker_roles),
            min_quote_chars=self.settings.query.answer.min_quote_chars,
        )

    def _check(
        self, draft: Draft, intent: Intent, plan: Plan, evidence: Evidence, block: NumbersBlock
    ) -> PostcheckResult:
        """Post-check the model's claims (other drafts have nothing to check)."""
        if draft.status != LLM_DRAFT:
            return PostcheckResult()
        return postcheck(
            draft.claims, evidence, block, self.settings.query.answer, self._rules(intent, plan)
        )

    def _compose(
        self,
        intent: Intent,
        evidence: Evidence,
        block: NumbersBlock,
        draft: Draft,
        check: PostcheckResult,
        llm_down: bool,
    ) -> Answer:
        """Interpretation + numbers block + checked explanation + sources + notes."""
        warnings = [*draft.notes, *evidence.warnings]
        if llm_down and LLM_DOWN_NOTE not in warnings:
            warnings = [w for w in warnings if "language model" not in w] + [LLM_DOWN_NOTE]
        if draft.status == LLM_DRAFT:
            fallback = NO_GUIDANCE if intent.forward_looking else NO_EXPLANATION
            explanation = check.explanation or fallback
            shown = self._claims_text(check) or explanation
            if not check.kept:  # nothing from the passages survived the check
                warnings = [
                    NO_USABLE_PASSAGE_NOTE if w == NO_TRANSCRIPT_NOTE else w for w in warnings
                ]
            if check.dropped:
                reasons = ", ".join(sorted({d["reason"].split(":")[0] for d in check.dropped}))
                warnings.append(
                    f"{len(check.dropped)} claim(s) of the model's explanation were removed "
                    f"by the check ({reasons})."
                )
        else:
            explanation = shown = draft.text or ""
        aliases = list(dict.fromkeys([*block.shown_aliases, *check.cited]))
        citations = build_citations(evidence, aliases)
        parts = [intent.interpretation]
        if block.text:
            parts += ["", block.text]
        if explanation:
            heading = "What management said" if intent.forward_looking else "What the filings say"
            parts += ["", f"**{heading}**", shown]
        parts += ["", "**Sources**", *self._sources(citations)]
        if warnings:
            parts += ["", "**Notes**", *[f"- {w}" for w in warnings]]
        return Answer(
            text="\n".join(parts),
            numbers_block=block.text,
            explanation=explanation,
            citations=citations,
            interpretation=intent.interpretation,
            warnings=warnings,
            dropped=check.dropped,
            claims=[c.to_json() for c in check.kept],
        )

    @staticmethod
    def _claims_text(check: PostcheckResult) -> str:
        """Kept claims -> one bullet per claim with its quote under it."""
        lines = []
        for claim in check.kept:
            lines += [f"- {claim.sentence}", f"  > “{claim.quote.strip()}”"]
        return "\n".join(lines)

    @staticmethod
    def _sources(citations: list[Citation]) -> list[str]:
        """Citations -> one 'Sources' line each."""
        lines = []
        for c in citations:
            d = c.detail
            if c.kind == "metric":
                what = f"metric {d['metric_id']}, period ending {d['period_end']}, {d['basis']}"
            elif c.kind == "fact":
                what = f"fact {d['concept']}, filing {d['filing_id']}, filed {d['filed_on']}"
            else:
                what = f"{d['header']}; filing {d['filing_id']}"
            lines.append(f"- [{c.alias}] {c.id} ({what})")
        return lines

    @staticmethod
    def _retrieved_ids(evidence: Evidence) -> list[str]:
        """Every ID the evidence holds (facts, metric values, chunks)."""
        return [
            *evidence.fact_aliases.values(),
            *evidence.metric_aliases.values(),
            *evidence.chunk_aliases.values(),
        ]

    def _log(
        self,
        question: str,
        intent: dict[str, Any],
        plan: dict[str, Any],
        retrieved: list[str],
        prompt_version: str,
        model: str,
        answer: str,
        cited: list[str],
        check: dict[str, Any],
    ) -> str | None:
        """Write the query_log row (when enabled) -> its query_id."""
        if not self.settings.logging.query_log_enabled:
            return None
        query_id = new_id(PREFIX_QUERY)
        with transaction(self.engine) as conn:
            conn.execute(
                sa.insert(tables.query_log).values(
                    query_id=query_id,
                    question=question,
                    intent_json=json.dumps(intent, default=str),
                    plan_json=json.dumps(plan, default=str),
                    retrieved_ids_json=json.dumps(retrieved),
                    prompt_version=prompt_version,
                    model=model,
                    answer=answer,
                    cited_ids_json=json.dumps(cited),
                    postcheck_json=json.dumps(check, default=str),
                    user_feedback=None,
                    created_at=utc_now_iso(),
                )
            )
        return query_id


def build_service(
    llm: LLMClient | None = None,
    availability: Availability | None = None,
    embedder: Embedder | None = None,
    use_configured_llm: bool = False,
    engine: Engine | None = None,
) -> QueryService:
    """The service for the real database, registries and (optionally) the configured LLM server."""
    settings = get_settings()
    taxonomy: Taxonomy = load_taxonomy()
    registry: MetricRegistry = load_metrics(taxonomy)
    if llm is None and use_configured_llm and settings.llm.model:
        llm = OpenAICompatClient(settings.llm)
    parser = build_parser(llm, availability or DbAvailability(engine))
    fmt = Formatter(settings, registry, taxonomy)
    return QueryService(
        parser=parser,
        planner=Planner(registry, taxonomy, parser.intents, settings),
        retriever=Retriever(
            embedder or SentenceTransformerEmbedder(settings.narrative),
            settings,
            registry,
            taxonomy,
            engine,
        ),
        answerer=Answerer(llm, parser.intents),
        builder=NumbersBlockBuilder(fmt, registry, taxonomy, settings.defaults.display_unit),
        settings=settings,
        llm=llm,
        engine=engine,
    )
