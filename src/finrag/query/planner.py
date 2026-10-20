"""Intent -> Plan: which figures to fetch and which narrative sources to search, in which order."""

from __future__ import annotations

from finrag.core.config import ConfigError, Settings
from finrag.core.fiscal import components_of, period_to_label, shift_period
from finrag.core.types import Period, PeriodType
from finrag.metrics.registry import MetricRegistry
from finrag.narrative.search import SearchRequest
from finrag.normalize.taxonomy import Taxonomy
from finrag.query.intents import Intents
from finrag.query.models import Intent, MetricRequest, Plan

TRANSCRIPT = "transcript"
RESULTS = "results"
NARRATIVE_TYPES = ("explanation", "narrative")
BASES = ("consolidated", "standalone")


def other_basis(basis: str) -> str:
    """consolidated <-> standalone."""
    return BASES[1] if basis == BASES[0] else BASES[0]


def instant_of(period: Period) -> Period:
    """A duration -> the balance-sheet date it ends on."""
    return Period(period.end, period.end, PeriodType.INSTANT)


def transcript_quarters(period: Period) -> list[Period]:
    """A period -> the quarters whose earnings calls are searched (H1 = Q1 + Q2, FY = Q1..Q4)."""
    return components_of(period) or [period]


def results_quarters(period: Period) -> list[Period]:
    """A period -> the quarter(s) whose results filing covers it (all results chunks are Q)."""
    if period.type is PeriodType.Q:
        return [period]
    last = components_of(period)[-1]
    return [last]


class Planner:
    """Builds the plan; holds the registries it needs to name things."""

    def __init__(
        self, registry: MetricRegistry, taxonomy: Taxonomy, intents: Intents, settings: Settings
    ) -> None:
        self.registry = registry
        self.taxonomy = taxonomy
        self.intents = intents
        self.settings = settings
        known = set(registry.order()) | set(taxonomy.concepts)
        for metric, others in intents.companions.items():
            unknown = ({metric} | set(others)) - known
            if unknown:
                raise ConfigError(f"intents.companions names unknown metrics: {sorted(unknown)}")

    def plan(self, intent: Intent) -> Plan:
        """Intent -> Plan (requests for the numbers, SearchRequests for explanation/narrative)."""
        plan = Plan(intent=intent, unavailable_sources=list(intent.unavailable_sources))
        self._numbers(intent, plan)
        if intent.question_type in NARRATIVE_TYPES:
            self._narrative(intent, plan)
        return plan

    # -- numbers -----------------------------------------------------------------------------

    def _bases(self, intent: Intent) -> list[str]:
        """The bases shown: both for a basis comparison, else the one chosen."""
        if intent.comparison == "basis" or intent.basis is None:
            first = self.settings.defaults.basis
            return [first, other_basis(first)]
        return [intent.basis]

    def _numbers(self, intent: Intent, plan: Plan) -> None:
        """Requests for the figures: prior periods, the other basis and companions."""
        bases = self._bases(intent)
        offer_other = intent.comparison != "basis"
        for metric in intent.metrics:
            for period in intent.periods:
                for basis in bases:
                    plan.requests.append(MetricRequest(metric, period, basis))
                if intent.comparison == "yoy":
                    plan.requests.append(
                        MetricRequest(
                            metric, shift_period(period, years=-1), bases[0], "prior_year"
                        )
                    )
                elif intent.comparison == "sequential":
                    plan.requests.append(
                        MetricRequest(
                            metric, shift_period(period, periods=-1), bases[0], "previous_period"
                        )
                    )
                if offer_other:
                    plan.requests.append(
                        MetricRequest(metric, period, other_basis(bases[0]), "other_basis")
                    )
        if intent.question_type == "explanation":
            asked = set(intent.metrics)
            for metric in intent.metrics:
                for companion in self.intents.companions.get(metric, []):
                    if companion in asked:
                        continue
                    asked.add(companion)
                    for period in intent.periods:
                        plan.requests.append(
                            MetricRequest(companion, period, bases[0], "companion")
                        )

    # -- narrative ---------------------------------------------------------------------------

    def _query_text(self, intent: Intent) -> str:
        """Question + metric names + synonyms (the narrative search text)."""
        words = [intent.question]
        for metric in intent.metrics:
            if metric in self.registry:
                m = self.registry.get(metric)
                words += [m.name, *m.synonyms[:3]]
            else:
                words += self.intents.concept_synonyms.get(metric, [metric])[:3]
        return " ".join(words)

    def _narrative(self, intent: Intent, plan: Plan) -> None:
        """Search requests in source order: transcripts first, then the results text."""
        k = self.settings.query.retrieval.k_candidates
        text = self._query_text(intent)
        hints = [s for s in intent.sections if s not in self.intents.topical_section_hints]
        topical = [s for s in intent.sections if s in self.intents.topical_section_hints]
        if (
            intent.unavailable_sources
            and not intent.sections
            and intent.question_type == "narrative"
        ):
            return  # asks only for sources that are not indexed: nothing to search
        calls: list[Period] = []
        results: list[Period] = []
        if intent.forward_looking and intent.search_quarters:
            # guidance is given on the latest calls, whatever horizon it is about
            calls = list(intent.search_quarters)
            results = [intent.search_quarters[-1]]
        for period in [] if calls else intent.periods:
            for q in transcript_quarters(period):
                if q not in calls:
                    calls.append(q)
            for q in results_quarters(period):
                if q not in results:
                    results.append(q)
        if not calls and not results:
            return
        if intent.exploratory:
            # no metric, no cue: the filed results text as it is, every section (audit text too)
            plan.searches.append(
                SearchRequest(
                    intent.entity_isin, intent.question, None, results, None, [RESULTS], k
                )
            )
            plan.search_groups.append(self._group(1, RESULTS, results, None))
            return
        order = 0
        roles = list(self.intents.management_speaker_roles) if intent.management_only else None
        if not hints and calls:
            order += 1
            plan.searches.append(
                SearchRequest(intent.entity_isin, text, None, calls, None, [TRANSCRIPT], k, roles)
            )
            plan.search_groups.append(self._group(order, TRANSCRIPT, calls, None, roles))
        # the auditor's text is searched only when the question is about the audit (it names a hint)
        sections = hints or list(self.intents.default_results_sections)
        for extra in topical:
            if extra not in sections:
                sections.append(extra)
        order += 1
        plan.searches.append(
            SearchRequest(intent.entity_isin, text, sections, results, None, [RESULTS], k)
        )
        plan.search_groups.append(self._group(order, RESULTS, results, sections))

    @staticmethod
    def _group(
        order: int,
        source: str,
        periods: list[Period],
        sections: list[str] | None,
        roles: list[str] | None = None,
    ) -> dict:
        """One search group as logged: its order, source type, quarters, sections and speakers."""
        return {
            "order": order,
            "source_type": source,
            "periods": [period_to_label(p) for p in periods],
            "sections": sections,
            **({"speaker_roles": roles} if roles else {}),
        }
