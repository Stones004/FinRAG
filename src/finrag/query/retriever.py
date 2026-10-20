"""Plan -> Evidence: stored metric values and facts, restatements, and narrative chunks."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import Settings
from finrag.core.db import tables, transaction
from finrag.core.fiscal import period_to_label
from finrag.core.logging import get_logger
from finrag.core.types import text_to_decimal
from finrag.facts.current_view import get_fact
from finrag.metrics.query import get_metric
from finrag.metrics.registry import MetricRegistry
from finrag.narrative.embedder import Embedder
from finrag.narrative.search import IndexOutOfDate, SearchHit, search
from finrag.normalize.taxonomy import Taxonomy
from finrag.query.mmr import select_mmr
from finrag.query.models import Entry, Evidence, MetricRequest, Plan
from finrag.query.planner import TRANSCRIPT, instant_of

log = get_logger("query.retriever")


class Retriever:
    """Executes a plan against metric_values, the fact store and the narrative index."""

    def __init__(
        self,
        embedder: Embedder,
        settings: Settings,
        registry: MetricRegistry,
        taxonomy: Taxonomy,
        engine: Engine | None = None,
    ) -> None:
        self.embedder = embedder
        self.settings = settings
        self.registry = registry
        self.taxonomy = taxonomy
        self.engine = engine

    def retrieve(self, plan: Plan) -> Evidence:
        """Plan -> Evidence with local aliases F1.. (figures) and N1.. (chunks)."""
        evidence = Evidence()
        self._numbers(plan, evidence)
        self._alias_figures(evidence)
        self._narrative(plan, evidence)
        return evidence

    # -- numbers -----------------------------------------------------------------------------

    def _numbers(self, plan: Plan, evidence: Evidence) -> None:
        """Every request -> an Entry (with a value, or with the reason there is none)."""
        isin = plan.intent.entity_isin
        for request in plan.requests:
            entry = Entry(request)
            if request.metric in self.registry:
                self._metric(isin, request, entry)
            else:
                self._concept(isin, request, entry)
            if self._dropped(entry):
                continue
            evidence.entries.append(entry)
            if entry.gap and request.role != "companion":
                evidence.missing.append(f"{self._label(request)}: {entry.gap}")

    @staticmethod
    def _dropped(entry: Entry) -> bool:
        """The other basis, and companion figures, are shown only when they carry a value."""
        request = entry.request
        value = entry.metric_value.value if entry.metric_value else None
        if entry.fact is not None:
            value = entry.fact.value
        if request.role == "other_basis":
            # a figure that falls back to the main basis is the same figure, not another basis
            mv = entry.metric_value
            return value is None or (mv is not None and mv.basis_used != request.basis)
        if request.role == "companion":
            return value is None or value == 0
        return False

    def _label(self, request: MetricRequest) -> str:
        """Request -> 'Operating margin Q1 FY27 (consolidated)'."""
        if request.metric in self.registry:
            name = self.registry.get(request.metric).name
        else:
            concept = self.taxonomy.concepts.get(request.metric)
            name = concept.label if concept else request.metric
        return f"{name} {period_to_label(request.period)} ({request.basis})"

    def _metric(self, isin: str, request: MetricRequest, entry: Entry) -> None:
        """A registry metric -> its stored value (a not-computed row keeps its reason)."""
        metric = self.registry.get(request.metric)
        if metric.is_instant:
            period = instant_of(request.period)
        elif request.period.type in metric.period_types:
            period = request.period
        else:
            kinds = ", ".join(t.value for t in metric.period_types)
            entry.gap = (
                f"not defined for a {request.period.type.value} period (defined for {kinds})"
            )
            return
        value = get_metric(isin, request.metric, period, request.basis, self.engine)
        if value is None:
            entry.gap = (
                f"no balance sheet at {period.end}"
                if metric.is_instant
                else "never computed for this period"
            )
            return
        entry.metric_value = value
        entry.restatements = self._metric_restatements(value)

    def _concept(self, isin: str, request: MetricRequest, entry: Entry) -> None:
        """A taxonomy concept -> its current fact and the restatements of that key."""
        concept = self.taxonomy.concepts.get(request.metric)
        period = request.period
        if concept is not None and concept.period_kind == "instant":
            period = instant_of(period)
        fact = get_fact(isin, request.metric, period, request.basis, engine=self.engine)
        if fact is None:
            entry.gap = "no reported value for this period and basis"
            return
        entry.fact = fact
        entry.restatements = self._restatements(isin, fact)

    def _restatements(self, isin: str, fact) -> list[dict]:
        """Restatement rows of a fact's key, each with both values and the filings behind them."""
        r = tables.restatements
        with transaction(self.engine) as conn:
            rows = list(
                conn.execute(
                    sa.select(r)
                    .where(
                        r.c.isin == isin,
                        r.c.concept == fact.concept,
                        r.c.period_start == fact.period.start.isoformat(),
                        r.c.period_end == fact.period.end.isoformat(),
                        r.c.basis == fact.basis,
                    )
                    .order_by(r.c.id)
                )
            )
            pairs = [
                (r_.concept, r_.old_fact_id, r_.new_fact_id, r_.abs_diff, r_.flagged) for r_ in rows
            ]
            return self._pairs(conn, pairs)

    def _metric_restatements(self, value) -> list[dict]:
        """The restated inputs a metric value recorded (details), with the filings behind them."""
        pairs = [
            (d["concept"], d["old_fact_id"], d["new_fact_id"], d["abs_diff"], True)
            for d in value.details.get("restatements", [])
        ]
        with transaction(self.engine) as conn:
            return self._pairs(conn, pairs)

    @staticmethod
    def _pairs(conn, pairs: list[tuple]) -> list[dict]:
        """(concept, old fact, new fact, abs diff, flagged) tuples -> restatement dicts."""
        c, f = tables.canonical_facts, tables.filings
        out = []
        for concept, old_id, new_id, abs_diff, flagged in pairs:
            versions = {}
            for label, fact_id in (("old", old_id), ("new", new_id)):
                versions[label] = conn.execute(
                    sa.select(c.c.value, c.c.source_filing_id, f.c.filed_on, f.c.submission)
                    .join(f, f.c.filing_id == c.c.source_filing_id)
                    .where(c.c.fact_id == fact_id)
                ).first()
            if versions["old"] is None or versions["new"] is None:
                continue
            out.append(
                {
                    "concept": concept,
                    "old_value": text_to_decimal(versions["old"].value),
                    "new_value": text_to_decimal(versions["new"].value),
                    "old_filing": versions["old"].source_filing_id,
                    "new_filing": versions["new"].source_filing_id,
                    "old_filed_on": versions["old"].filed_on,
                    "new_filed_on": versions["new"].filed_on,
                    "new_submission": versions["new"].submission,
                    "abs_diff": text_to_decimal(str(abs_diff)),
                    "flagged": bool(flagged),
                    "new_fact_id": new_id,
                }
            )
        return out

    def _alias_figures(self, evidence: Evidence) -> None:
        """Give every figure that has a value an alias F1..Fn (capped by max_facts_in_prompt)."""
        cap = self.settings.query.answer.max_facts_in_prompt
        count = 0
        for entry in evidence.entries:
            if entry.metric_value is None and entry.fact is None:
                continue
            if count >= cap:
                evidence.warnings.append(
                    f"only the first {cap} figures are shown; ask for fewer metrics or periods"
                )
                break
            count += 1
            entry.alias = f"F{count}"
            if entry.metric_value is not None:
                evidence.metric_values.append(entry.metric_value)
                evidence.metric_aliases[entry.alias] = entry.metric_value.metric_value_id
            else:
                assert entry.fact is not None
                evidence.facts.append(entry.fact)
                evidence.fact_aliases[entry.alias] = entry.fact.fact_id

    # -- narrative ---------------------------------------------------------------------------

    def _narrative(self, plan: Plan, evidence: Evidence) -> None:
        """Search groups in order; MMR picks each group's chunks; later groups only fill slots."""
        slots = self.settings.query.answer.max_chunks_in_prompt
        lam = self.settings.query.retrieval.mmr_lambda
        chosen: list[SearchHit] = []
        for request in plan.searches:
            try:
                hits = search(request, self.embedder, self.settings, self.engine)
            except IndexOutOfDate as exc:
                evidence.warnings.append(f"the narrative index is out of date: {exc}")
                continue
            is_transcript = request.source_types == [TRANSCRIPT]
            if is_transcript and not hits:
                evidence.transcripts_missing = True
            if hits and not evidence.top_scores:
                evidence.top_scores = dict(hits[0].scores)
            seen = {h.chunk_id for h in chosen}
            hits = [h for h in hits if h.chunk_id not in seen]
            take = slots - len(chosen)
            if take > 0:
                chosen += select_mmr(hits, request.query_text, self.embedder, take, lam)
        evidence.chunks = chosen
        for n, hit in enumerate(chosen, start=1):
            evidence.chunk_aliases[f"N{n}"] = hit.chunk_id
