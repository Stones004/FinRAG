"""Question -> Intent or Clarification: resolvers, LLM fallback for weak fields, decision."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from finrag.core.config import QuerySettings, Settings, get_settings
from finrag.core.logging import get_logger
from finrag.core.types import Period
from finrag.llm.base import LLMClient
from finrag.metrics.registry import MetricRegistry, load_metrics
from finrag.normalize.taxonomy import Taxonomy, load_taxonomy
from finrag.query import llm_classifier
from finrag.query.availability import Availability, DbAvailability
from finrag.query.entity_resolver import EntityInfo, load_entities, resolve_entity
from finrag.query.intents import Intents, load_intents
from finrag.query.metric_resolver import MetricMatch, MetricResolution, MetricResolver
from finrag.query.models import (
    AMBIGUOUS_METRIC,
    CONFIRM,
    LOW_CONFIDENCE,
    MISSING_METRIC,
    MULTI_ENTITY,
    NO_ENTITY,
    NO_NARRATIVE_MATCH,
    PERIOD_NOT_AVAILABLE,
    PROCEED,
    UNKNOWN_COMPANY,
    UNKNOWN_METRIC,
    Candidate,
    Clarification,
    Intent,
    basis_phrase,
)
from finrag.query.period_resolver import (
    PeriodParse,
    PeriodResolution,
    describe_period,
    latest_quarters,
    parse_periods,
    resolve_periods,
)
from finrag.query.qtype import QuestionKind, classify_question
from finrag.query.text import Working
from finrag.watchlist.registry import today_in_app_tz

log = get_logger("query.intent")

_EXPLICIT_PERIOD_KINDS = ("quarter", "half", "nine_months", "fiscal_year")
_SAMPLE_METRICS = 5
_BASES = ("consolidated", "standalone")


class IntentParser:
    """Parses one question at a time against the watch list, the registries and the stored data."""

    def __init__(
        self,
        entities: list[EntityInfo],
        registry: MetricRegistry,
        taxonomy: Taxonomy,
        intents: Intents,
        settings: Settings,
        availability: Availability,
        llm: LLMClient | None = None,
    ) -> None:
        self.entities = entities
        self.registry = registry
        self.taxonomy = taxonomy
        self.intents = intents
        self.settings = settings
        self.query: QuerySettings = settings.query
        self.avail = availability
        self.llm = llm
        self.metrics = MetricResolver(registry, intents, settings.query)
        self._starters = {s.casefold() for s in intents.question_starters}
        self._by_isin = {e.isin: e for e in entities}

    # -- public ------------------------------------------------------------------------------

    def parse(self, question: str, today: date | None = None) -> Intent | Clarification:
        """Question -> Intent (to answer, perhaps after "Is this right?") or a Clarification."""
        today = today or today_in_app_tz(self.settings)
        work = Working(question, self.intents)
        periods = parse_periods(work, self.intents)
        named_bases = self._take_bases(work)
        found = resolve_entity(work, self.entities, self.query, self._starters)
        if found.problem is not None:
            return self._entity_clarification(question, found.problem, found)
        entity = self._by_isin[found.isin or ""]
        entity_conf = found.confidence
        metric_res = self.metrics.resolve(work, entity.sector_type)
        kind = self._kind(work, periods, metric_res)
        exploratory = self._exploratory(work, kind, metric_res)
        if exploratory:
            kind = replace(kind, question_type="narrative")

        llm_used = False
        if self._weak(entity_conf, metric_res, kind, found.defaulted):
            choice = self._ask_llm(question, entity, entity_conf, metric_res, kind)
            if choice is not None:
                llm_used = True
                entity, entity_conf, metric_res = self._apply(
                    choice, entity, entity_conf, metric_res
                )
                kind = self._kind(work, periods, metric_res)
                if exploratory:
                    kind = replace(kind, question_type="narrative")

        needs_metric = kind.question_type != "narrative"
        if kind.question_type is None or (needs_metric and not metric_res.ids):
            return self._metric_clarification(question, entity, metric_res)

        ids = metric_res.ids
        metric_ids = [i for i in ids if i in self.registry]
        concept_ids = [i for i in ids if i not in self.registry]
        resolved = resolve_periods(
            periods,
            entity.isin,
            metric_ids,
            concept_ids,
            self.avail,
            self.query,
            today,
            forward=kind.forward,
            transcript_default=kind.management or kind.question_type == "narrative",
        )
        if resolved.future and not kind.forward:
            return self._period_clarification(question, entity, resolved)
        return self._decide(
            question,
            entity,
            entity_conf,
            llm_used,
            metric_res,
            kind,
            resolved,
            named_bases,
            found.defaulted,
            exploratory,
        )

    # -- steps -------------------------------------------------------------------------------

    def _take_bases(self, work: Working) -> list[str]:
        """Consume basis words -> the bases named, in order of mention."""
        hits: list[tuple[int, str]] = []
        for basis, words in self.intents.basis_words.items():
            for word in words:
                for span in work.find(word):
                    hits.append((span.start, basis))
                    work.consume(span)
        return list(dict.fromkeys(b for _, b in sorted(hits)))

    def _kind(
        self, work: Working, parse: PeriodParse, metric_res: MetricResolution
    ) -> QuestionKind:
        """Question type from keywords, comparison signals and whether a metric was named."""
        explicit = [m for m in parse.mentions if m.kind in _EXPLICIT_PERIOD_KINDS]
        signal = bool(
            parse.comparison
            or len(explicit) >= 2
            or any(m.kind == "last_n_years" for m in parse.mentions)
            or parse.trend_word
        )
        return classify_question(work, self.intents, bool(metric_res.ids), signal)

    def _exploratory(self, work: Working, kind: QuestionKind, metric_res: MetricResolution) -> bool:
        """No metric, no question cue, but distinctive words of its own -> search the filings.

        The words left after the company, the period and the basis are taken out, minus starters
        and `explore_stopwords`, must number `explore_min_content_words`; and none of them may be
        part of a metric's name ("Sharpe ratio" asks for a metric the registry does not have).
        Whether anything in the filings matches is decided after the search (the service).
        """
        if kind.question_type is not None or metric_res.ids or self.metrics.generic_words(work):
            return False
        skip = self._starters | {w.casefold() for w in self.intents.explore_stopwords}
        words = {
            t.text
            for t in work.tokens()
            if t.text.isalpha() and len(t.text) >= 3 and t.text not in skip
        }
        return len(words) >= self.query.intent.explore_min_content_words

    def _weak(
        self, entity_conf: float, metric_res: MetricResolution, kind: QuestionKind, defaulted: bool
    ) -> bool:
        """True if the LLM fallback should look at the entity or the metric."""
        if self.llm is None or not self.query.intent.llm_fallback_enabled:
            return False
        auto = self.query.intent.auto_proceed_threshold
        if not defaulted and entity_conf < auto:
            return True
        if kind.question_type == "narrative":
            return False
        return metric_res.problem is not None or metric_res.confidence < auto

    def _ask_llm(
        self,
        question: str,
        entity: EntityInfo,
        entity_conf: float,
        metric_res: MetricResolution,
        kind: QuestionKind,
    ) -> llm_classifier.Classified | None:
        """Shortlists for the weak fields -> the LLM's validated choice (or None)."""
        auto = self.query.intent.auto_proceed_threshold
        size = self.query.intent.shortlist_size
        entities = [Candidate(e.isin, e.name) for e in self.entities][:size]
        if entity_conf >= auto:
            entities = [Candidate(entity.isin, entity.name)]
        weak_metric = kind.question_type != "narrative" and (
            metric_res.problem is not None or metric_res.confidence < auto
        )
        metrics: list[Candidate] = []
        if weak_metric:
            pool = {c.id: c for c in metric_res.candidates}
            for match in metric_res.matches:
                if match.kind == "metric":
                    pool.setdefault(match.id, self._candidate(match.id))
            if not pool:
                pool = {c.id: c for c in self.metrics.shortlist(entity.sector_type)[:size]}
            metrics = list(pool.values())[:size]
        assert self.llm is not None
        return llm_classifier.classify(self.llm, question, entities, metrics, self.intents)

    def _candidate(self, metric_id: str) -> Candidate:
        metric = self.registry.get(metric_id)
        return Candidate(metric_id, metric.name, 0.0, metric.description)

    def _apply(
        self,
        choice: llm_classifier.Classified,
        entity: EntityInfo,
        entity_conf: float,
        metric_res: MetricResolution,
    ) -> tuple[EntityInfo, float, MetricResolution]:
        """The LLM's choice replaces the weak fields, with the confidence mapped from its word.

        A field that was already strong is left alone; where the LLM agrees with the resolver the
        higher of the two confidences is kept.
        """
        auto = self.query.intent.auto_proceed_threshold
        if choice.entity is not None and entity_conf < auto:
            same = choice.entity == entity.isin
            entity = self._by_isin[choice.entity]
            entity_conf = max(entity_conf, choice.confidence) if same else choice.confidence
        if choice.metric is not None and (metric_res.problem or metric_res.confidence < auto):
            same = metric_res.ids == [choice.metric]
            confidence = (
                max(metric_res.confidence, choice.confidence) if same else choice.confidence
            )
            metric_res = MetricResolution(
                matches=[MetricMatch(choice.metric, "metric", "(llm)", confidence, 0)]
            )
        return entity, entity_conf, metric_res

    # -- decision ----------------------------------------------------------------------------

    def _decide(
        self,
        question: str,
        entity: EntityInfo,
        entity_conf: float,
        llm_used: bool,
        metric_res: MetricResolution,
        kind: QuestionKind,
        resolved: PeriodResolution,
        named_bases: list[str],
        entity_defaulted: bool,
        exploratory: bool = False,
    ) -> Intent | Clarification:
        """Combine the fields: overall confidence = minimum, then proceed / confirm / clarify."""
        ids = metric_res.ids
        comparison = resolved.comparison
        basis, basis_note = self._basis(entity, ids, named_bases, comparison)
        if len(named_bases) == 2 and comparison is None:
            comparison, basis, basis_note = "basis", None, "both bases"
        forward = kind.forward
        horizon = resolved.future if forward else []
        quarters: list[Period] = []
        if forward and resolved.defaulted and resolved.latest_filed is not None:
            quarters = latest_quarters(
                resolved.latest_filed, self.query.retrieval.guidance_quarters
            )
        fields = {"entity": entity_conf, "period": resolved.confidence}
        if ids:
            fields["metric"] = metric_res.confidence
        overall = min(fields.values())
        defaults = [
            d
            for d in (
                "entity" if entity_defaulted else None,
                "period" if resolved.defaulted else None,
                "period_transcript" if resolved.transcript_default else None,
                f"basis:{basis_note}" if basis_note and basis_note != "named" else None,
                "question_type" if not kind.matched else None,
            )
            if d
        ]
        intent = Intent(
            question=question,
            entity_isin=entity.isin,
            entity_name=entity.name,
            metrics=ids,
            periods=resolved.periods,
            comparison=comparison,
            question_type=kind.question_type or "",
            basis=basis,
            sections=kind.sections,
            confidence=overall,
            decision=PROCEED,
            defaults_applied=defaults,
            unavailable_sources=kind.unavailable,
            field_confidence=fields,
            notes=resolved.notes,
            sector_type=entity.sector_type,
            llm_used=llm_used,
            forward_looking=forward,
            horizon=horizon,
            search_quarters=quarters,
            management_only=kind.management,
            exploratory=exploratory,
        )
        auto, floor = self.query.intent.auto_proceed_threshold, self.query.intent.clarify_threshold
        if overall < floor:
            return Clarification(
                question=question,
                reason=LOW_CONFIDENCE,
                message="I am not sure what you are asking. " + self.describe(intent),
                candidates=[Candidate(entity.isin, entity.name, entity_conf)]
                + [self._candidate(i) for i in ids if i in self.registry],
                confidence=overall,
                partial=intent.to_json(),
            )
        if overall < auto:
            intent.decision = CONFIRM
        intent.interpretation = self.describe(intent)
        return intent

    def _basis(
        self, entity: EntityInfo, ids: list[str], named: list[str], comparison: str | None
    ) -> tuple[str | None, str]:
        """Basis by precedence: named, the metric's preferred, the entity default, then settings."""
        if comparison == "basis":
            return None, "both bases"
        if len(named) == 1:
            return named[0], "named"
        for metric_id in ids:
            if metric_id in self.registry:
                preferred = self.registry.get(metric_id).preferred_basis
                if preferred:
                    return preferred, "metric_preferred"
        if entity.default_basis in _BASES:
            return entity.default_basis, "entity_default"
        return self.settings.defaults.basis, "settings_default"

    # -- text --------------------------------------------------------------------------------

    def metric_name(self, metric_id: str) -> str:
        """Metric or concept ID -> display name."""
        if metric_id in self.registry:
            return self.registry.get(metric_id).name
        concept = self.taxonomy.concepts.get(metric_id)
        return concept.label if concept else metric_id

    def describe(self, intent: Intent) -> str:
        """Intent -> the interpretation line shown above every answer."""
        parts = [intent.entity_name]
        names = " and ".join(self.metric_name(m) for m in intent.metrics)
        if intent.forward_looking:
            topic = f" on {names}" if names else ""
            when = ", ".join(describe_period(p) for p in intent.horizon)
            parts.append(
                f"management guidance and outlook{topic}"
                + (f" for {when}, which is not filed yet" if when else "")
            )
            if intent.periods:
                shown = ", ".join(describe_period(p) for p in intent.periods)
                parts.append(f"last reported actuals shown for reference: {shown}")
        else:
            parts.append(
                names
                or (
                    "a search of the filed results text" if intent.exploratory else "narrative only"
                )
            )
            if intent.periods:
                parts.append(", ".join(describe_period(p) for p in intent.periods))
        if intent.metrics and (intent.comparison == "basis" or intent.basis):
            parts.append(basis_phrase(intent))
        line = "Reading your question as: " + " — ".join(parts) + "."
        for note in intent.notes:
            line += f" {note}."
        if "period_transcript" in intent.defaults_applied:
            line += (
                " No period was named, so the latest period with an earnings-call transcript "
                "is used."
            )
        elif "period" in intent.defaults_applied and not intent.forward_looking:
            line += " No period was named, so the latest with data is shown."
        if intent.decision == CONFIRM:
            line += " Is this right?"
        return line

    def _period_clarification(self, question: str, entity: EntityInfo, resolved) -> Clarification:
        """A named period that is not filed yet -> say so and offer the latest filed period."""
        asked = ", ".join(describe_period(p) for p in resolved.future)
        latest = resolved.latest_filed
        assert latest is not None
        shown = describe_period(latest)
        text = (
            f"{asked} is not available yet: the latest filed period for {entity.name} is "
            f"{shown}. Ask for {shown.split(' (')[0]}, or leave the period out and I will use it."
        )
        label = shown.split(" (")[0]
        return Clarification(
            question,
            PERIOD_NOT_AVAILABLE,
            text,
            [Candidate(label, f"{shown}, the latest filed")],
            0.0,
            {"isin": entity.isin, "not_available": [describe_period(p) for p in resolved.future]},
        )

    def no_match_clarification(self, intent: Intent) -> Clarification:
        """An exploratory search found nothing close enough -> ask for a metric or a topic."""
        entity = self._by_isin[intent.entity_isin]
        candidates = self.metrics.shortlist(entity.sector_type)[:_SAMPLE_METRICS]
        names = ", ".join(c.name for c in candidates)
        text = (
            f"I found nothing in {intent.entity_name}'s filed results text that matches that "
            f"closely enough to answer it. Name a metric or a topic, for example: {names}."
        )
        return Clarification(
            intent.question,
            NO_NARRATIVE_MATCH,
            text,
            candidates,
            0.0,
            {"isin": intent.entity_isin},
        )

    def _names(self) -> str:
        return ", ".join(e.name for e in self.entities)

    def _entity_clarification(self, question: str, problem: str, found) -> Clarification:
        """Entity problem -> clarification that lists the tracked companies."""
        if problem == MULTI_ENTITY:
            names = " and ".join(c.name for c in found.candidates)
            text = f"One company at a time, please. Which one do you mean: {names}?"
        elif problem == UNKNOWN_COMPANY:
            text = f"I do not track {', '.join(found.unknown_names)}. I track: {self._names()}."
        else:
            problem = NO_ENTITY
            text = f"Which company do you mean? I track: {self._names()}."
        return Clarification(question, problem, text, found.candidates, found.confidence)

    def _metric_clarification(
        self, question: str, entity: EntityInfo, res: MetricResolution
    ) -> Clarification:
        """Metric problem -> clarification with the candidates."""
        problem = res.problem or MISSING_METRIC
        if problem == AMBIGUOUS_METRIC:
            names = ", ".join(c.name for c in res.candidates)
            text = f"Which metric do you mean: {names}?"
            candidates = res.candidates
        elif problem == UNKNOWN_METRIC:
            names = ", ".join(c.name for c in res.candidates[:_SAMPLE_METRICS])
            text = (
                f"I have no metric called '{' '.join(res.unknown_words)}'. "
                f"The nearest I have: {names}."
            )
            candidates = res.candidates
        else:
            candidates = self.metrics.shortlist(entity.sector_type)[:_SAMPLE_METRICS]
            names = ", ".join(c.name for c in candidates)
            text = f"What would you like to know about {entity.name}? For example: {names}."
        return Clarification(
            question, problem, text, candidates, res.confidence, {"isin": entity.isin}
        )


def build_parser(
    llm: LLMClient | None = None, availability: Availability | None = None
) -> IntentParser:
    """The parser for the real database and registries (settings, watch list, metrics, intents)."""
    settings = get_settings()
    taxonomy = load_taxonomy()
    return IntentParser(
        entities=load_entities(),
        registry=load_metrics(taxonomy),
        taxonomy=taxonomy,
        intents=load_intents(taxonomy),
        settings=settings,
        availability=availability or DbAvailability(),
        llm=llm,
    )
