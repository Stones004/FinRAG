"""Maximal marginal relevance over narrative hits (candidates are re-embedded on CPU)."""

from __future__ import annotations

from finrag.narrative.embedder import Embedder
from finrag.narrative.search import SearchHit


def select_mmr(
    hits: list[SearchHit],
    query_text: str,
    embedder: Embedder,
    n: int,
    mmr_lambda: float,
) -> list[SearchHit]:
    """Candidates -> up to n hits that are relevant to the query and unlike each other.

    Each step picks the candidate maximising lambda * relevance - (1 - lambda) * (largest
    similarity to a hit already chosen). Relevance and similarity are cosine similarities of the
    texts re-embedded with the M08 embedder (`SearchHit` carries no vector). With n or fewer
    candidates the input is returned unchanged; ties go to the earlier (better RRF) hit.
    """
    if len(hits) <= n:
        return list(hits)
    vectors = embedder.embed([h.text for h in hits])
    query = embedder.embed_query(query_text)
    relevance = vectors @ query
    similarity = vectors @ vectors.T
    chosen: list[int] = []
    remaining = list(range(len(hits)))
    while remaining and len(chosen) < n:

        def score(i: int) -> float:
            redundancy = max((similarity[i, j] for j in chosen), default=0.0)
            return float(mmr_lambda * relevance[i] - (1 - mmr_lambda) * redundancy)

        best = max(remaining, key=lambda i: (score(i), -i))
        chosen.append(best)
        remaining.remove(best)
    return [hits[i] for i in chosen]
