"""Map one parsed fact to a canonical concept: XBRL QName, then label, then user resolutions."""

from __future__ import annotations

from dataclasses import dataclass, field

from finrag.core.config import get_settings
from finrag.normalize.models import METHOD_LABEL, METHOD_MANUAL, METHOD_STANDARD
from finrag.normalize.taxonomy import KnownUnmapped, Taxonomy, normalise_label
from finrag.parsing.models import ParsedFact

KEY_QNAME = "qname"
KEY_LABEL = "label"
REASON_USER_IGNORED = "user_ignored"


@dataclass
class Resolutions:
    """Decisions made through `finrag review`: concept mappings and ignored keys."""

    qnames: dict[tuple[str, str], str] = field(default_factory=dict)  # (family, QName) -> concept
    # (statement | "", normalised label) -> concept; a statement-less key is for old facts
    labels: dict[tuple[str, str], str] = field(default_factory=dict)
    ignored_qnames: set[tuple[str, str]] = field(default_factory=set)  # (family, QName)
    ignored_labels: set[tuple[str, str]] = field(default_factory=set)  # (statement | "", label)


@dataclass(frozen=True)
class Mapped:
    """The fact maps to this concept."""

    concept: str
    method: str


@dataclass(frozen=True)
class NotApplicable:
    """The fact maps to a concept that does not apply to the entity's sector."""

    concept: str
    method: str


@dataclass(frozen=True)
class Known:
    """The fact is understood but deliberately not mapped (taxonomy known_unmapped, or ignored)."""

    reason: str
    entry: KnownUnmapped | None = None


@dataclass(frozen=True)
class Unmapped:
    """No mapping: goes to the review queue, with a fuzzy suggestion when one is close."""

    key: str
    key_type: str
    suggestion: dict[str, object] | None = None
    statement: str | None = None  # labels: the statement of the page the label was read on


Outcome = Mapped | NotApplicable | Known | Unmapped


def _suggest(label: str, taxonomy: Taxonomy, min_score: int) -> dict[str, object] | None:
    """Best fuzzy label match at or above the threshold -> suggestion dict (never auto-applied)."""
    from rapidfuzz import fuzz, process

    choices = taxonomy.label_choices()
    if not choices:
        return None
    best = process.extractOne(
        label, [text for text, _ in choices], scorer=fuzz.token_sort_ratio, score_cutoff=min_score
    )
    if best is None:
        return None
    text, score, index = best
    return {"concept": choices[index][1], "label": text, "score": round(float(score), 1)}


class Mapper:
    """Applies the taxonomy and the user's resolutions to parsed facts."""

    def __init__(
        self,
        taxonomy: Taxonomy,
        resolutions: Resolutions | None = None,
        fuzzy_min_score: int | None = None,
    ) -> None:
        self.taxonomy = taxonomy
        self.resolutions = resolutions or Resolutions()
        self.fuzzy_min_score = (
            get_settings().normalize.fuzzy_suggest_min_score
            if fuzzy_min_score is None
            else fuzzy_min_score
        )

    def _applies(self, concept: str, sector: str, method: str) -> Mapped | NotApplicable:
        """Mapped, unless the concept is restricted to other sectors."""
        sectors = self.taxonomy.concepts[concept].sectors
        if sectors is not None and sector not in {s.value for s in sectors}:
            return NotApplicable(concept, method)
        return Mapped(concept, method)

    def map_xbrl(self, concept_raw: str, family: str, sector: str) -> Outcome:
        """XBRL QName -> concept (standard), known-unmapped, manual resolution or Unmapped."""
        concept = self.taxonomy.by_qname.get((family, concept_raw))
        if concept is not None:
            return self._applies(concept, sector, METHOD_STANDARD)
        entry = self.taxonomy.known.get((family, concept_raw))
        if entry is not None:
            return Known(entry.reason, entry)
        manual = self.resolutions.qnames.get((family, concept_raw))
        if manual is not None:
            return self._applies(manual, sector, METHOD_MANUAL)
        if (family, concept_raw) in self.resolutions.ignored_qnames:
            return Known(REASON_USER_IGNORED)
        return Unmapped(concept_raw, KEY_QNAME)

    def map_label(self, label_raw: str, sector: str, statement: str | None = None) -> Outcome:
        """Statement label (+ the statement of its page) -> concept, Known or Unmapped.

        A label only maps to a concept of its own statement ("Finance costs" on a cash-flow page
        is not the P&L line). A page whose statement type is itself an unmapped reason (segment)
        gives Known(reason).
        """
        key = normalise_label(label_raw)
        if statement is not None and statement in self.taxonomy.reasons:
            return Known(statement)
        scope = statement or ""
        manual = self.resolutions.labels.get((scope, key))
        if manual is not None:
            return self._applies(manual, sector, METHOD_MANUAL)
        concept = self.taxonomy.by_label.get(key)
        if concept is not None and statement in (None, self.taxonomy.concepts[concept].statement):
            return self._applies(concept, sector, METHOD_LABEL)
        if (scope, key) in self.resolutions.ignored_labels:
            return Known(REASON_USER_IGNORED)
        suggestion = None if concept else _suggest(key, self.taxonomy, self.fuzzy_min_score)
        return Unmapped(key, KEY_LABEL, suggestion, statement)

    def map_fact(self, fact: ParsedFact, family: str, sector: str) -> Outcome:
        """ParsedFact (+ the filing's taxonomy family and the entity's sector) -> Outcome."""
        if fact.concept_raw:
            return self.map_xbrl(fact.concept_raw, family, sector)
        if fact.label_raw:
            return self.map_label(fact.label_raw, sector, fact.statement_type)
        return Unmapped("", KEY_LABEL)
