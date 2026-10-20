"""ask(question) -> Answer or Clarification, one query_log row per call; feedback on an answer."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date
from typing import Any

import numpy as np
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
    LLM_UNREADABLE,
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
    postcheck_selections,
)
from finrag.query.retriever import Retriever
from finrag.query.selection import LeadInContext, Picked, attribution
from finrag.query.sentences import number_sentences
from finrag.query.text import load_vocab

log = get_logger("query.service")

RELATED_HEADING = "Related disclosures (not summarised; not stated as the reason for the change)"
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
        related = self._related(intent, plan, evidence, draft, check)
        answer = self._compose(intent, evidence, block, draft, check, llm_down, related)
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
            self._check_log(draft, check, related),
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

    def _check_log(self, draft: Draft, check: PostcheckResult, related: list[Picked]) -> dict:
        """What the check did, for query_log; the raw reply too when the settings flag is on."""
        record: dict[str, Any] = {
            "kept": [c.to_json() for c in check.kept],
            "picked": [p.to_json() for p in check.picked],
            "dropped": check.dropped,
            "lead_ins_dropped": check.lead_ins_dropped,
            "related": [p.to_json() for p in related],
            "status": draft.status,
        }
        if self.settings.logging.query_log_raw_reply and draft.response is not None:
            record["raw_reply"] = draft.response.text
        return record

    def _related(
        self, intent: Intent, plan: Plan, evidence: Evidence, draft: Draft, check: PostcheckResult
    ) -> list[Picked]:
        """Nothing the model chose survived but retrieval was strong -> the best sentences.

        Among the retrieved passages' sentences (as stored), those that share most words with the
        search text, then those closest to the question by embedding; a sentence that shares no
        word and is not close is left out (a greeting is not a disclosure). They are shown as
        related disclosures and never as the reason for anything.
        """
        retrieval = self.settings.query.retrieval
        if draft.status not in (LLM_DRAFT, LLM_UNREADABLE) or check.survived:
            return []
        if (
            not evidence.chunks
            or evidence.top_scores.get("dense", 0.0) < retrieval.related_min_dense_score
        ):
            return []
        floor = self.settings.query.answer.min_quote_chars
        intents = self.answerer.intents
        sentences = [
            s
            for s in number_sentences(evidence, frozenset(intents.sentence_abbreviations)).values()
            if len(s.text) >= floor
        ]
        if not sentences:
            return []
        skip = (
            {w.casefold() for w in intents.explore_stopwords}
            | {w.casefold() for w in intents.question_starters}
            | {w.casefold() for w in re.findall(r"[A-Za-z]+", intent.entity_name)}
        )
        text = plan.searches[0].query_text if plan.searches else intent.question
        terms = {
            w for w in re.findall(r"[a-z]{3,}", text.casefold()) if w not in skip and w != "fy"
        }
        embedder = self.retriever.embedder
        vectors = np.asarray(embedder.embed([s.text for s in sentences]), dtype=float)
        query = np.asarray(embedder.embed_query(intent.question), dtype=float)
        norms = np.linalg.norm(vectors, axis=1) * (np.linalg.norm(query) or 1.0)
        cosine = vectors @ query / np.where(norms == 0, 1.0, norms)
        overlap = [
            len(
                terms & {w.removesuffix("s") for w in re.findall(r"[a-z]{3,}", s.text.casefold())}
                | terms & set(re.findall(r"[a-z]{3,}", s.text.casefold()))
            )
            for s in sentences
        ]
        close = retrieval.related_min_dense_score
        keep = [i for i in range(len(sentences)) if overlap[i] > 0 or cosine[i] >= close]
        keep.sort(key=lambda i: (-overlap[i], -cosine[i], i))
        return [Picked(sentences[i]) for i in keep[: retrieval.related_sentences]]

    def _check(
        self, draft: Draft, intent: Intent, plan: Plan, evidence: Evidence, block: NumbersBlock
    ) -> PostcheckResult:
        """Post-check what the model chose (other drafts have nothing to check)."""
        if draft.status != LLM_DRAFT:
            return PostcheckResult()
        if draft.selections:
            rules = self._rules(intent, plan)
            rules = replace(rules, min_quote_chars=self.settings.query.answer.min_quote_chars)
            sentences = number_sentences(
                evidence, frozenset(self.answerer.intents.sentence_abbreviations)
            )
            figures = {n for numbers in block.allowed.values() for n in numbers}
            context = LeadInContext(
                figure_numbers=frozenset(figures),
                figure_words=frozenset(w.casefold() for w in re.findall(r"[A-Za-z]+", block.text)),
                known_names=frozenset(
                    w.casefold() for w in re.findall(r"[A-Za-z]+", intent.entity_name)
                ),
                vocab=load_vocab()
                | frozenset(w.casefold() for w in self.answerer.intents.explore_stopwords),
            )
            return postcheck_selections(
                draft.selections,
                sentences,
                block,
                rules,
                context,
                self.settings.query.answer.show_lead_ins,
            )
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
        related: list[Picked] | None = None,
    ) -> Answer:
        """Interpretation + numbers block + checked explanation + sources + notes."""
        warnings = [*draft.notes, *evidence.warnings]
        if llm_down and LLM_DOWN_NOTE not in warnings:
            warnings = [w for w in warnings if "language model" not in w] + [LLM_DOWN_NOTE]
        heading = "What management said" if intent.forward_looking else "What the filings say"
        if draft.status == LLM_DRAFT:
            fallback = NO_GUIDANCE if intent.forward_looking else NO_EXPLANATION
            explanation = check.explanation or fallback
            shown = self._claims_text(check) or explanation
            if related:
                explanation = shown = self._picked_text(related)
                heading = RELATED_HEADING
            if not check.survived:  # nothing from the passages survived the check
                warnings = [
                    NO_USABLE_PASSAGE_NOTE if w == NO_TRANSCRIPT_NOTE else w for w in warnings
                ]
            if check.dropped:
                reasons = ", ".join(sorted({d["reason"].split(":")[0] for d in check.dropped}))
                what = (
                    "sentence(s) the model chose"
                    if check.picked or related
                    else "claim(s) of the model's explanation"
                )
                warnings.append(
                    f"{len(check.dropped)} {what} were removed by the check ({reasons})."
                )
            if check.lead_ins_dropped:
                reasons = ", ".join(
                    sorted({d["reason"].split(":")[0] for d in check.lead_ins_dropped})
                )
                warnings.append(
                    f"{len(check.lead_ins_dropped)} lead-in(s) were removed by the check "
                    f"({reasons}); the sentence is shown as stored."
                )
        else:
            explanation = shown = draft.text or ""
        shown_related = [p.sentence.alias for p in related or []]
        aliases = list(dict.fromkeys([*block.shown_aliases, *check.cited, *shown_related]))
        citations = build_citations(evidence, aliases)
        parts = [intent.interpretation]
        if block.text:
            parts += ["", block.text]
        if explanation:
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
            claims=[c.to_json() for c in check.kept] + [p.to_json() for p in check.picked],
        )

    def _picked_text(self, picked: list[Picked]) -> str:
        """Sentences -> one bullet each: the lead-in (or who said it) and the stored sentence."""
        roles = frozenset(r.casefold() for r in self.answerer.intents.management_speaker_roles)
        lines = []
        for p in picked:
            s = p.sentence
            who = attribution(s, roles)
            head = p.lead_in.rstrip(".") if p.lead_in else who
            tail = f" — {who}" if p.lead_in and s.hit.source_type == "transcript" else ""
            lines += [f"- {head} [{s.alias}]{'' if p.lead_in else ':'}", f"  > “{s.text}”{tail}"]
        return "\n".join(lines)

    def _claims_text(self, check: PostcheckResult) -> str:
        """Kept claims or selected sentences -> one bullet each with the quote under it."""
        if check.picked:
            return self._picked_text(check.picked)
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
