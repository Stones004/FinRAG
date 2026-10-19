"""Reading and writing the `chunks` table."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from finrag.core.db import tables
from finrag.core.ids import chunk_id
from finrag.core.types import utc_now_iso
from finrag.narrative.models import DraftChunk


def meta_json(meta: dict[str, Any]) -> str | None:
    """Chunk metadata -> JSON text (None when empty)."""
    clean = {k: v for k, v in meta.items() if v is not None}
    return json.dumps(clean, sort_keys=True, ensure_ascii=False) if clean else None


def count_for_filing(conn: Connection, filing_id: str) -> int:
    """Number of chunks stored for a filing."""
    c = tables.chunks
    return int(
        conn.execute(sa.select(sa.func.count()).where(c.c.filing_id == filing_id)).scalar() or 0
    )


def delete_for_filing(conn: Connection, filing_id: str) -> int:
    """Remove a filing's chunks -> how many were removed."""
    result = conn.execute(sa.delete(tables.chunks).where(tables.chunks.c.filing_id == filing_id))
    return int(result.rowcount or 0)


def replace_for_filing(
    conn: Connection,
    filing: sa.Row,
    source_type: str,
    fiscal_year: str | None,
    drafts: Sequence[DraftChunk],
) -> list[str]:
    """Replace a filing's chunks with these drafts (deterministic IDs) -> the new chunk IDs."""
    delete_for_filing(conn, filing.filing_id)
    now = utc_now_iso()
    rows, ids = [], []
    for seq, draft in enumerate(drafts):
        cid = chunk_id(filing.filing_id, draft.page, seq)
        ids.append(cid)
        rows.append(
            {
                "chunk_id": cid,
                "filing_id": filing.filing_id,
                "isin": filing.isin,
                "fiscal_year": fiscal_year,
                "section": draft.section,
                "source_type": source_type,
                "period_start": filing.period_start,
                "period_end": filing.period_end,
                "period_type": filing.period_type,
                "speaker": draft.speaker,
                "speaker_role": draft.speaker_role,
                "page": draft.page,
                "seq": seq,
                "text": draft.text,
                "token_count": draft.token_count,
                "created_at": now,
                "meta_json": meta_json(draft.meta),
            }
        )
    if rows:
        conn.execute(sa.insert(tables.chunks), rows)
    return ids


def texts_for_isin(conn: Connection, isin: str) -> list[tuple[str, str]]:
    """All (chunk_id, text) of one ISIN, in chunk_id order (the order of the indexes)."""
    c = tables.chunks
    rows = conn.execute(
        sa.select(c.c.chunk_id, c.c.text).where(c.c.isin == isin).order_by(c.c.chunk_id)
    )
    return [(r.chunk_id, r.text) for r in rows]
