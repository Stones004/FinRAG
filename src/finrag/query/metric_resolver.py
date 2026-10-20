"""Question -> metrics or taxonomy concepts: longest synonym first, then fuzzy, then ambiguity."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from finrag.core.config import ConfigError, QuerySettings
from finrag.metrics.registry import MetricRegistry
from finrag.query.intents import Intents
from finrag.query.models import AMBIGUOUS_METRIC, MISSING_METRIC, UNKNOWN_METRIC, Candidate
from finrag.query.text import Working, load_vocab

_MAX_NGRAM_TOKENS = 4
_MIN_GENERIC_WORD_CHARS = 4
_WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class MetricMatch:
    """One metric or concept the question names."""

    id: str
    kind: str  # metric | concept
    phrase: str
    score: float  # 1.0 exact, else fuzzy score / 100
    start: int


@dataclass
class MetricResolution:
    """Which metrics/concepts the question asks for and how sure we are."""

    matches: list[MetricMatch] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    problem: str | None = None  # missing_metric | unknown_metric | ambiguous_metric
    unknown_words: list[str] = field(default_factory=list)

    @property
    def ids(self) -> list[str]:
        """Matched IDs in the order asked, without repeats."""
        return list(dict.fromkeys(m.id for m in sorted(self.matches, key=lambda m: m.start)))

    @property
    def confidence(self) -> float:
        """The weakest match (0.0 when nothing matched)."""
        return min((m.score for m in self.matches), default=0.0)


class MetricResolver:
    """Phrase tables from the metric registry and the concept synonyms of the intents registry."""

    def __init__(self, registry: MetricRegistry, intents: Intents, settings: QuerySettings) -> None:
        self._registry = registry
        self._settings = settings
        self._phrases: dict[str, list[tuple[str, str]]] = {}  # phrase -> [(kind, id)]
        for phrase, ids in registry.synonyms().items():
            self._phrases[phrase] = [("metric", i) for i in ids]
        for concept, words in intents.concept_synonyms.items():
            for word in words:
                self._phrases.setdefault(word.casefold(), []).append(("concept", concept))
        self._by_word: dict[str, set[str]] = {}
        for phrase, entries in self._phrases.items():
            for kind, ident in entries:
                if kind != "metric":
                    continue
                for word in _WORD.findall(phrase):
                    if len(word) >= _MIN_GENERIC_WORD_CHARS:
                        self._by_word.setdefault(word, set()).add(ident)
        self._starters = {s.casefold() for s in intents.question_starters}
        self._growth_words = list(intents.growth_words)
        self._growth = dict(intents.growth_counterparts)
        unknown = {m for m in self._growth.values() if m not in registry}
        if unknown:
            raise ConfigError(
                f"intents.growth_counterparts names unknown metrics: {sorted(unknown)}"
            )

    def _pick(self, entries: list[tuple[str, str]], sector: str | None) -> list[tuple[str, str]]:
        """Entries sharing a phrase -> the ones for this sector (all if none apply)."""
        metrics = [e for e in entries if e[0] == "metric"]
        if len(metrics) > 1 and sector:
            fit = [e for e in metrics if self._registry.get(e[1]).applies_to(sector)]
            if fit:
                return fit + [e for e in entries if e[0] != "metric"]
        return entries

    def _applicable(self, ids: set[str], sector: str | None) -> list[str]:
        """Metric IDs applicable to the sector (all if none)."""
        order = {m: n for n, m in enumerate(self._registry.order())}
        if sector:
            fit = [i for i in ids if self._registry.get(i).applies_to(sector)]
            if fit:
                return sorted(fit, key=lambda i: order[i])
        return sorted(ids, key=lambda i: order[i])

    def _candidate(self, metric_id: str, score: float = 0.0) -> Candidate:
        metric = self._registry.get(metric_id)
        return Candidate(metric_id, metric.name, score, metric.description)

    def resolve(self, work: Working, sector: str | None) -> MetricResolution:
        """Working question -> metrics (consumes the words that named them)."""
        result = MetricResolution()
        for phrase in sorted(self._phrases, key=lambda p: (-len(p), p)):
            for span in work.find(phrase):
                for kind, ident in self._pick(self._phrases[phrase], sector):
                    result.matches.append(MetricMatch(ident, kind, phrase, 1.0, span.start))
                work.consume(span)
        if result.matches:
            self._promote_growth(work, result)
            return result
        fuzzy = self._fuzzy(work)
        if fuzzy:
            result.matches = fuzzy
            return result
        return self._explain_absence(work, sector, result)

    def generic_words(self, work: Working) -> list[str]:
        """Unconsumed words that are part of some metric's name ("ratio", "margin", "growth")."""
        return [
            t.text
            for t in work.tokens()
            if {t.text, t.text.removesuffix("s")} & self._by_word.keys()
            and t.text not in self._starters
        ]

    def _promote_growth(self, work: Working, result: MetricResolution) -> None:
        """'growth' beside a metric that has a year-on-year growth metric -> that growth metric.

        "loans growth" names the concept `bank_advances` (the growth metric's own synonym is
        "loan growth"); the word "growth" says the question is about the change, not the level.
        """
        spans = [span for word in self._growth_words for span in work.find(word)]
        if not spans:
            return
        growth_ids = set(self._growth.values())
        if any(m.id in growth_ids for m in result.matches):
            return
        promoted = [
            MetricMatch(self._growth[m.id], "metric", m.phrase, m.score, m.start)
            if m.id in self._growth
            else m
            for m in result.matches
        ]
        if promoted != result.matches:
            result.matches = promoted
            for span in spans:
                work.consume(span)

    def _fuzzy(self, work: Working) -> list[MetricMatch]:
        """Typos: the best n-gram against every phrase of at least fuzzy_min_chars."""
        intent = self._settings.intent
        phrases = [p for p in self._phrases if len(p) >= intent.fuzzy_min_chars]
        best: tuple[float, int, str, object] | None = None
        for gram in work.ngrams(intent.fuzzy_min_chars, _MAX_NGRAM_TOKENS):
            found = process.extractOne(
                gram.text, phrases, scorer=fuzz.ratio, score_cutoff=intent.fuzzy_min_score
            )
            if found is None:
                continue
            phrase, score, _ = found
            rank = (score, len(phrase))
            if best is None or rank > (best[0], len(best[2])):
                best = (score, gram.start, phrase, gram)
        if best is None:
            return []
        score, start, phrase, gram = best
        work.consume(gram)  # type: ignore[arg-type]
        return [
            MetricMatch(ident, kind, phrase, score / 100, start)
            for kind, ident in self._phrases[phrase]
        ]

    def _explain_absence(
        self, work: Working, sector: str | None, result: MetricResolution
    ) -> MetricResolution:
        """Nothing matched: unknown word, a generic word shared by metrics, or nothing asked."""
        vocab = load_vocab()
        words = [t.text for t in work.tokens()]
        unknown = [
            w
            for w in words
            if w.isalpha() and len(w) >= 3 and w not in vocab and w not in self._starters
        ]
        if unknown:
            result.problem = UNKNOWN_METRIC
            result.unknown_words = unknown
            result.candidates = self.nearest(" ".join(unknown), sector)
            return result
        generic = {
            m
            for w in words
            for form in {w, w.removesuffix("s")}
            if form in self._by_word and w not in self._starters
            for m in self._by_word[form]
        }
        applicable = self._applicable(generic, sector)
        if len(applicable) > 1:
            result.problem = AMBIGUOUS_METRIC
            result.candidates = [self._candidate(i) for i in applicable]
            return result
        result.problem = MISSING_METRIC
        return result

    def nearest(self, text: str, sector: str | None) -> list[Candidate]:
        """Text -> the closest registry metrics (for an unknown-metric clarification)."""
        names = {p: e for p, e in self._phrases.items() if any(k == "metric" for k, _ in e)}
        scored = process.extract(
            text,
            list(names),
            scorer=fuzz.token_set_ratio,
            limit=self._settings.intent.shortlist_size * 3,
        )
        seen: dict[str, Candidate] = {}
        for phrase, score, _ in scored:
            for kind, ident in names[phrase]:
                if kind == "metric" and ident not in seen:
                    seen[ident] = self._candidate(ident, score / 100)
        pool = self._applicable(set(seen), sector)
        ordered = sorted((seen[i] for i in pool), key=lambda c: -c.score)
        return ordered[: self._settings.intent.shortlist_size]

    def shortlist(self, sector: str | None) -> list[Candidate]:
        """Metrics applicable to the sector, for the LLM fallback."""
        ids = {i for entries in self._phrases.values() for k, i in entries if k == "metric"}
        pool = self._applicable(ids, sector)
        return [self._candidate(i) for i in pool]
