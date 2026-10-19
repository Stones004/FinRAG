"""Search: SQL filters, then BM25 and dense retrieval inside the filtered set, fused by RRF."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.config import ConfigError, Settings, get_settings
from finrag.core.db import tables, transaction
from finrag.core.fiscal import period_to_label
from finrag.core.types import Period, PeriodType
from finrag.narrative.bm25_index import load_bm25
from finrag.narrative.embedder import Embedder
from finrag.narrative.indexer import index_folder
from finrag.narrative.vector_index import load_vectors, top_ids


class IndexOutOfDate(Exception):
    """Raised when stored chunks are not in the indexes (run `finrag narrative index`)."""


@dataclass(frozen=True)
class SearchRequest:
    """What to search: one company, a query, and filters that are never relaxed."""

    isin: str
    query_text: str
    sections: list[str] | None = None
    periods: list[Period] | None = None  # exact periods (start, end, type) of the source filing
    fiscal_years: list[str] | None = None
    source_types: list[str] | None = None
    k: int | None = None


@dataclass(frozen=True)
class SearchHit:
    """One retrieved chunk with what a citation needs."""

    chunk_id: str
    filing_id: str
    section: str
    source_type: str
    speaker: str | None
    speaker_role: str | None
    firm: str | None
    page: int | None
    end_page: int | None
    concept: str | None
    basis: str | None
    fiscal_year: str | None
    period_start: str | None
    period_end: str | None
    period_type: str | None
    period_label: str | None
    text: str
    scores: dict[str, float] = field(default_factory=dict)


def _filters(request: SearchRequest) -> list[Any]:
    """Request -> SQL conditions on the chunks table."""
    c = tables.chunks
    conditions: list[Any] = [c.c.isin == request.isin]
    if request.sections:
        conditions.append(c.c.section.in_(request.sections))
    if request.fiscal_years:
        conditions.append(c.c.fiscal_year.in_(request.fiscal_years))
    if request.source_types:
        conditions.append(c.c.source_type.in_(request.source_types))
    if request.periods is not None:
        conditions.append(
            sa.or_(
                *[
                    sa.and_(
                        c.c.period_end == p.end.isoformat(),
                        c.c.period_type == PeriodType(p.type).value,
                        sa.or_(c.c.period_start.is_(None), c.c.period_start == p.start.isoformat()),
                    )
                    for p in request.periods
                ]
            )
            if request.periods
            else sa.false()
        )
    return conditions


def _matches(row: sa.Row, request: SearchRequest) -> bool:
    """The same filters re-checked in Python: a hit outside them is never returned."""
    if row.isin != request.isin:
        return False
    if request.sections and row.section not in request.sections:
        return False
    if request.fiscal_years and row.fiscal_year not in request.fiscal_years:
        return False
    if request.source_types and row.source_type not in request.source_types:
        return False
    if request.periods is not None:
        return any(
            row.period_end == p.end.isoformat()
            and row.period_type == PeriodType(p.type).value
            and row.period_start in (None, p.start.isoformat())
            for p in request.periods
        )
    return True


def _label(row: sa.Row) -> str | None:
    """Filing period of a chunk -> 'Q1 FY27', or None when it has no clean label."""
    try:
        from datetime import date

        return period_to_label(
            Period(
                date.fromisoformat(row.period_start or row.period_end),
                date.fromisoformat(row.period_end),
                PeriodType(row.period_type),
            )
        )
    except (ValueError, TypeError, KeyError):
        return None


def _hit(row: sa.Row, scores: dict[str, float]) -> SearchHit:
    """Chunk row + scores -> SearchHit (metadata read from meta_json)."""
    meta = json.loads(row.meta_json) if row.meta_json else {}
    return SearchHit(
        chunk_id=row.chunk_id,
        filing_id=row.filing_id,
        section=row.section,
        source_type=row.source_type,
        speaker=row.speaker,
        speaker_role=row.speaker_role,
        firm=meta.get("firm"),
        page=row.page,
        end_page=meta.get("end_page"),
        concept=meta.get("concept"),
        basis=meta.get("basis"),
        fiscal_year=row.fiscal_year,
        period_start=row.period_start,
        period_end=row.period_end,
        period_type=row.period_type,
        period_label=_label(row),
        text=row.text,
        scores=scores,
    )


def search(
    request: SearchRequest,
    embedder: Embedder,
    settings: Settings | None = None,
    engine: Engine | None = None,
) -> list[SearchHit]:
    """Request -> up to k hits, best first; always inside the filters, possibly empty."""
    settings = settings or get_settings()
    retrieval = settings.narrative.retrieval
    if retrieval.reranker_enabled:
        raise ConfigError("narrative.retrieval.reranker_enabled: no reranker is built in v1")
    with transaction(engine) as conn:
        rows = {
            r.chunk_id: r
            for r in conn.execute(sa.select(tables.chunks).where(*_filters(request)))
            if _matches(r, request)
        }
    if not rows:
        return []
    folder = index_folder(settings, request.isin)
    bm25, vectors = load_bm25(folder), load_vectors(folder)
    if bm25 is None or vectors is None:
        raise IndexOutOfDate(f"no index for {request.isin}: run `finrag narrative index`")
    allowed = set(rows)
    if allowed - set(bm25.ids) or allowed - set(vectors.ids):
        raise IndexOutOfDate("some chunks are not indexed: run `finrag narrative index`")
    keyword = top_ids(
        bm25.ids, bm25.scores(request.query_text), allowed, retrieval.top_k_bm25, True
    )
    query = embedder.embed_query(request.query_text)
    dense = top_ids(vectors.ids, vectors.scores(query), allowed, retrieval.top_k_dense)
    fused: dict[str, float] = {}
    for ranking in (keyword, dense):
        for rank, (cid, _) in enumerate(ranking, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (retrieval.rrf_k + rank)
    keyword_scores, dense_scores = dict(keyword), dict(dense)
    order = sorted(fused, key=lambda cid: (-fused[cid], cid))
    hits = [
        _hit(
            rows[cid],
            {
                "rrf": fused[cid],
                "bm25": keyword_scores.get(cid, 0.0),
                "dense": dense_scores.get(cid, 0.0),
            },
        )
        for cid in order
        if _matches(rows[cid], request)
    ]
    return hits[: request.k or retrieval.final_k]
