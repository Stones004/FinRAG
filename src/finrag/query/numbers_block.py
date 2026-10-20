"""The numbers block: the only place numbers enter an answer. A deterministic template.

One line per figure with value, change, basis and alias; under it the restatements, accounting
policy changes, validation warnings and fallbacks recorded with the value. A figure that was not
computed shows its reason; a figure with nothing behind it shows what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from finrag.core.fiscal import period_to_label
from finrag.core.types import MetricStatus, Period, PeriodType
from finrag.metrics.registry import MetricRegistry
from finrag.normalize.taxonomy import Taxonomy
from finrag.query.formatter import Formatter, Rendered
from finrag.query.models import Entry, Evidence, Intent, Plan, basis_phrase
from finrag.query.period_resolver import describe_period

MAIN = "main"
ATTACHED = ("prior_year", "previous_period", "other_basis")


@dataclass
class NumbersBlock:
    """The block as markdown, and for each alias the numbers a sentence citing it may contain."""

    text: str
    allowed: dict[str, set[Decimal]] = field(default_factory=dict)
    shown_aliases: list[str] = field(default_factory=list)


class NumbersBlockBuilder:
    """Builds the block for one answer."""

    def __init__(
        self, fmt: Formatter, registry: MetricRegistry, taxonomy: Taxonomy, unit: str
    ) -> None:
        self.fmt = fmt
        self.registry = registry
        self.taxonomy = taxonomy
        self.unit = unit
        self._actual = False  # set per answer: a guidance question shows actuals, labelled

    # -- names -------------------------------------------------------------------------------

    def name(self, metric_id: str) -> str:
        """Metric or concept ID -> display name."""
        if metric_id in self.registry:
            return self.registry.get(metric_id).name
        concept = self.taxonomy.concepts.get(metric_id)
        return concept.label if concept else metric_id

    @staticmethod
    def when(period: Period) -> str:
        """Period -> 'Q1 FY27 (Apr-Jun 2026)', or a date for a balance-sheet instant."""
        if period.type is PeriodType.INSTANT:
            return f"at {period.end:%d %b %Y}"
        return describe_period(period)

    # -- values ------------------------------------------------------------------------------

    def value_of(self, entry: Entry) -> tuple[Decimal | None, Rendered | None]:
        """Entry -> (raw value, rendered value) or (None, None) when nothing is computed."""
        mv, fact = entry.metric_value, entry.fact
        if mv is not None and mv.value is not None:
            return mv.value, self.fmt.metric(mv.value, entry.request.metric)
        if fact is not None:
            return fact.value, self.fmt.concept(fact.value, entry.request.metric)
        return None, None

    # -- build -------------------------------------------------------------------------------

    def build(self, intent: Intent, plan: Plan, evidence: Evidence) -> NumbersBlock:
        """Intent, plan and evidence -> NumbersBlock."""
        block = NumbersBlock(text="")
        if not intent.metrics:
            return block  # the question asks for no figure: nothing to show or to say about them
        self._actual = intent.forward_looking
        lines = [f"**Figures** — {basis_phrase(intent)}; amounts in {self.unit}."]
        if intent.forward_looking:
            shown = ", ".join(self.when(p) for p in intent.periods) or "the latest period"
            ahead = ", ".join(self.when(p) for p in intent.horizon)
            lines.append(
                f"These are reported actuals for {shown}; they are not management guidance"
                + (f" for {ahead}." if ahead else ".")
            )
        entries = evidence.entries
        mains = [e for e in entries if e.request.role == MAIN]
        if not plan.requests:
            lines.append("- No data: no filed results were found for this company.")
        for entry in mains:
            lines += self._main(entry, entries, intent, block)
        context = [e for e in entries if e.request.role == "companion"]
        if context:
            lines.append("Context for the same period:")
            for entry in context:
                lines += self._line(entry, block, indent="- ")
        for entry in entries:
            if entry.request.role in ("prior_year", "previous_period") and entry.alias is None:
                lines.append(f"- No data for {self._label(entry)} (missing: {entry.gap})")
        for note in evidence.warnings:
            lines.append(f"- Note: {note}")
        block.text = "\n".join(lines)
        return block

    def _label(self, entry: Entry) -> str:
        r = entry.request
        return f"{self.name(r.metric)} {self.when(r.period)} ({r.basis})"

    def _line(self, entry: Entry, block: NumbersBlock, indent: str = "- ") -> list[str]:
        """One figure -> its line (value or reason), then its flags."""
        r = entry.request
        raw, shown = self.value_of(entry)
        actual = " (actual)" if self._actual else ""
        title = f"{self.name(r.metric)}, {self.when(r.period)}{actual}, {r.basis}"
        if entry.alias is None:
            return [f"{indent}No data for {title} (missing: {entry.gap})"]
        allowed = block.allowed.setdefault(entry.alias, set())
        block.shown_aliases.append(entry.alias)
        if shown is None:
            mv = entry.metric_value
            reason = mv.reason if mv is not None else "no value"
            lines = [f"{indent}[{entry.alias}] {title}: not computed — {reason}"]
        else:
            if shown.number is not None:
                allowed.add(shown.number)
            lines = [f"{indent}[{entry.alias}] {title}: {shown.text}"]
        return lines + self._flags(entry, allowed)

    def _main(
        self, entry: Entry, entries: list[Entry], intent: Intent, block: NumbersBlock
    ) -> list[str]:
        """A main figure, its change, its prior value and the other basis, in one group."""
        lines = self._line(entry, block)
        raw, _ = self.value_of(entry)
        r = entry.request
        allowed = block.allowed.get(entry.alias or "", set())
        display = self.fmt.display_of(r.metric)
        for other in entries:
            o = other.request
            if o.metric != r.metric or o.role not in ATTACHED:
                continue
            if o.role == "other_basis" and o.period == r.period and o.basis != r.basis:
                lines += self._line(other, block, indent="    Also on the other basis: ")
            elif o.role in ("prior_year", "previous_period") and o.basis == r.basis:
                prior_raw, prior_shown = self.value_of(other)
                if raw is not None and prior_raw is not None and prior_shown is not None:
                    change = self.fmt.change(raw, prior_raw, display)
                    prior = f"{self.when(o.period)}: {prior_shown.text} [{other.alias}]"
                    note = f"    Change vs {prior}"
                    if change is not None:
                        note += f" → {change.text}"
                        if change.number is not None:
                            allowed.add(change.number)
                    lines.append(note)
                    if prior_shown.number is not None:
                        allowed.add(prior_shown.number)
                    block.allowed.setdefault(other.alias or "", set()).add(
                        prior_shown.number or Decimal(0)
                    )
                    block.shown_aliases.append(other.alias or "")
        lines += self._period_changes(entry, entries, intent, display, allowed)
        return lines

    def _period_changes(
        self, entry: Entry, entries: list[Entry], intent: Intent, display: str, allowed: set
    ) -> list[str]:
        """Comparison across named periods: the change from the earlier period listed."""
        if intent.comparison not in ("periods", "trend"):
            return []
        r = entry.request
        same = sorted(
            (
                e
                for e in entries
                if e.request.role == MAIN
                and e.request.metric == r.metric
                and e.request.basis == r.basis
                and e.alias
            ),
            key=lambda e: e.request.period.end,
        )
        index = same.index(entry) if entry in same else 0
        if index == 0:
            return []
        earlier = same[index - 1]
        raw, _ = self.value_of(entry)
        earlier_raw, earlier_shown = self.value_of(earlier)
        if raw is None or earlier_raw is None or earlier_shown is None:
            return []
        change = self.fmt.change(raw, earlier_raw, display)
        if change is None:
            return []
        if change.number is not None:
            allowed.add(change.number)
        label = period_to_label(earlier.request.period)
        return [f"    Change vs {label} [{earlier.alias}]: {change.text}"]

    # -- flags -------------------------------------------------------------------------------

    def _flags(self, entry: Entry, allowed: set[Decimal]) -> list[str]:
        """Restatements, accounting-policy changes, warnings and fallbacks of one figure."""
        lines: list[str] = []
        mv = entry.metric_value
        for event in mv.basis_change if mv else []:
            lines.append(
                f"    Basis change: the periods straddle an {event['event_type'].replace('_', ' ')}"
                f" effective {event['event_date']} (event {event['event_id']}), so this is not "
                f"like for like. {event.get('citation', '')}".rstrip()
            )
        for row in entry.restatements:
            lines.append("    " + self._restatement(entry, row, allowed))
        if entry.restatements:
            lines.append("    No reason for the restatement is given in the stored filings.")
        if mv is not None:
            lines += self._metric_notes(mv, entry.request.basis)
            if mv.status is MetricStatus.NOT_COMPUTED:
                lines += self._blocked(mv)
        return lines

    def _restatement(self, entry: Entry, row: dict[str, Any], allowed: set[Decimal]) -> str:
        """A restatement row -> 'Restated: was X (filing, date); now Y (filing, date)'."""
        concept = row.get("concept", entry.request.metric)
        old = self.fmt.concept(row["old_value"], concept)
        new = self.fmt.concept(row["new_value"], concept)
        for rendered in (old, new):
            if rendered.number is not None:
                allowed.add(rendered.number)
        subject = ":" if entry.fact is not None else f" input {self.name(concept)}:"
        kind = row.get("new_submission") or "later filing"
        flag = "" if row["flagged"] else " (within the restatement tolerance)"
        return (
            f"Restated{subject} was {old.text} ({row['old_filing']}, filed {row['old_filed_on']}); "
            f"now {new.text} ({row['new_filing']}, {kind} filed {row['new_filed_on']}){flag}."
        )

    @staticmethod
    def _metric_notes(mv: Any, requested: str) -> list[str]:
        """Basis fallback, assumed zeros, owners' profit fallbacks, validation warnings."""
        lines: list[str] = []
        used = mv.basis_used
        if used != requested:
            lines.append(
                f"    Shown on the {used} basis: the {requested} filings report none of the inputs."
            )
        for item in mv.details.get("assumed_zero", []):
            lines.append(
                f"    Assumed 0 for {item['concept']} at {item['period_end']} (not reported)."
            )
        for item in mv.details.get("fallbacks", []):
            lines.append(f"    Used {item['used']} instead of {item['wanted']}: {item['why']}.")
        warnings = mv.warnings
        if warnings:
            grouped: dict[tuple[str, str], list[str]] = {}
            for w in warnings:
                grouped.setdefault((w["rule_id"], w.get("period_end", "")), []).append(
                    w.get("concept", "")
                )
            for (rule, day), concepts in grouped.items():
                names = ", ".join(sorted(set(filter(None, concepts)))) or "inputs"
                lines.append(f"    Validation warning ({rule}) on {names}, period ending {day}.")
        return lines

    @staticmethod
    def _blocked(mv: Any) -> list[str]:
        """The validation rule that blocked a metric, if that is why it was not computed."""
        blockers = mv.details.get("blocked_by", [])
        if not blockers:
            return []
        names = ", ".join(
            str(b.get("rule_id", b)) if isinstance(b, dict) else str(b) for b in blockers
        )
        return [f"    Blocked by validation rule: {names}."]
