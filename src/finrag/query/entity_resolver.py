"""Question -> company: watch-list aliases (exact), then fuzzy; one tracked company is default."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlalchemy as sa
from rapidfuzz import fuzz
from sqlalchemy.engine import Engine

from finrag.core.config import QuerySettings
from finrag.core.db import tables, transaction
from finrag.query.models import MULTI_ENTITY, NO_ENTITY, UNKNOWN_COMPANY, Candidate
from finrag.query.text import Working, normalize
from finrag.watchlist.master import NAME_NOISE_WORDS

_MIN_SHORT_NAME_CHARS = 3
_TITLE_WORD = re.compile(r"\b[A-Z][a-z]{2,}\b")
_MIN_SUGGESTION_SCORE = 60
_MAX_NGRAM_TOKENS = 3


@dataclass(frozen=True)
class EntityInfo:
    """An active watch-list company and every name it can be called."""

    isin: str
    symbol: str
    name: str
    default_basis: str
    sector_type: str
    aliases: frozenset[str]  # normalised, lower case


@dataclass
class EntityResolution:
    """Which company the question names, how sure we are, and what to offer otherwise."""

    isin: str | None = None
    confidence: float = 0.0
    candidates: list[Candidate] = field(default_factory=list)
    defaulted: bool = False
    problem: str | None = None  # no_entity | unknown_company | multi_entity
    unknown_names: list[str] = field(default_factory=list)


def load_entities(engine: Engine | None = None) -> list[EntityInfo]:
    """Active entities with their current aliases and derived short names."""
    e, a = tables.entities, tables.entity_aliases
    with transaction(engine) as conn:
        rows = list(conn.execute(sa.select(e).where(e.c.status == "active").order_by(e.c.isin)))
        alias_rows = list(conn.execute(sa.select(a).where(a.c.valid_to.is_(None))))
    entities: list[EntityInfo] = []
    first_words: dict[str, int] = {}
    names: dict[str, list[str]] = {}
    for row in rows:
        raw = {row.nse_symbol or "", row.name or ""} | {
            r.alias for r in alias_rows if r.isin == row.isin
        }
        trimmed = " ".join(w for w in (row.name or "").split() if w.upper() not in NAME_NOISE_WORDS)
        raw.add(trimmed)
        forms = {normalize(x).strip() for x in raw if x}
        names[row.isin] = sorted(f for f in forms if f)
        first = trimmed.split()[0].lower() if trimmed else ""
        if len(first) >= _MIN_SHORT_NAME_CHARS:
            first_words[first] = first_words.get(first, 0) + 1
    for row in rows:
        trimmed = " ".join(w for w in (row.name or "").split() if w.upper() not in NAME_NOISE_WORDS)
        first = trimmed.split()[0].lower() if trimmed else ""
        forms = set(names[row.isin])
        if first_words.get(first) == 1:
            forms.add(first)
        entities.append(
            EntityInfo(
                isin=row.isin,
                symbol=row.nse_symbol or row.isin,
                name=row.name or row.isin,
                default_basis=row.default_basis or "",
                sector_type=row.sector_type or "general",
                aliases=frozenset(forms),
            )
        )
    return entities


def _title_words(original: str, work: Working, starters: set[str]) -> list[str]:
    """Capitalised words of the question that are still unexplained and do not open a question."""
    leftover = {t.text for t in work.tokens()}
    found = []
    for word in _TITLE_WORD.findall(re.sub(r"['’]s\b", "", original)):
        low = word.lower()
        if low in starters or low not in leftover:
            continue
        found.append(word)
    return found


def resolve_entity(
    work: Working,
    entities: list[EntityInfo],
    settings: QuerySettings,
    starters: set[str],
) -> EntityResolution:
    """Working question -> company (consumes the words that named it).

    Exact alias match gives confidence 1.0; a fuzzy match gives score/100. Several companies named
    -> problem `multi_entity`; none named and one tracked company -> that company, `defaulted`.
    """
    exact: dict[str, int] = {}  # isin -> longest alias length matched
    spans: dict[str, list] = {}
    for entity in entities:
        for alias in sorted(entity.aliases, key=len, reverse=True):
            for span in work.find(alias):
                exact[entity.isin] = max(exact.get(entity.isin, 0), len(alias))
                spans.setdefault(entity.isin, []).append(span)
    if exact:
        for isin in exact:
            for span in sorted(spans[isin], key=lambda s: s.start - s.end)[:1]:
                work.consume(span)
        matched = [e for e in entities if e.isin in exact]
        return _decide(matched, 1.0, entities)

    fuzzy: dict[str, tuple[float, object]] = {}
    for gram in work.ngrams(settings.intent.fuzzy_min_chars, _MAX_NGRAM_TOKENS):
        for entity in entities:
            for alias in entity.aliases:
                if len(alias) < settings.intent.fuzzy_min_chars:
                    continue
                score = fuzz.ratio(gram.text, alias)
                if (
                    score >= settings.intent.fuzzy_min_score
                    and score > fuzzy.get(entity.isin, (0.0, None))[0]
                ):
                    fuzzy[entity.isin] = (score, gram)
    if fuzzy:
        matched = [e for e in entities if e.isin in fuzzy]
        score = min(fuzzy[e.isin][0] for e in matched) / 100
        for e in matched:
            work.consume(fuzzy[e.isin][1])  # type: ignore[arg-type]
        return _decide(matched, score, entities)

    unknown = _title_words(work.original, work, starters)
    if unknown:
        return EntityResolution(
            problem=UNKNOWN_COMPANY,
            candidates=_suggestions(work, entities),
            unknown_names=unknown,
        )
    if len(entities) == 1:
        return EntityResolution(isin=entities[0].isin, confidence=1.0, defaulted=True)
    return EntityResolution(problem=NO_ENTITY, candidates=_suggestions(work, entities))


def _decide(
    matched: list[EntityInfo], confidence: float, entities: list[EntityInfo]
) -> EntityResolution:
    """Companies matched -> one resolution (several = multi_entity)."""
    candidates = [Candidate(e.isin, e.name, confidence) for e in matched]
    if len(matched) > 1:
        return EntityResolution(confidence=confidence, candidates=candidates, problem=MULTI_ENTITY)
    return EntityResolution(isin=matched[0].isin, confidence=confidence, candidates=candidates)


def _suggestions(work: Working, entities: list[EntityInfo]) -> list[Candidate]:
    """Every tracked company, nearest first (offered when no company is named)."""
    text = work.text.strip()
    scored = [
        Candidate(
            e.isin, e.name, max((fuzz.partial_ratio(a, text) for a in e.aliases), default=0) / 100
        )
        for e in entities
    ]
    return sorted(scored, key=lambda c: -c.score)
