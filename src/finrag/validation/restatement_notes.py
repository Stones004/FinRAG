"""The sentence in which a results PDF says its comparative figures were restated.

A comparative that differs from the figure first filed is a restatement only if something explains
it: the arithmetic of the PDF still holds, or the company says so in the filing that printed it
("the figures for the previous periods have been restated accordingly"). This module finds the
second kind of evidence, in the text layer of a native-text PDF. A scan has no text layer to read
and is never searched: an OCR column cannot be a restated comparative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from finrag.core.db import tables, transaction

_SNIPPET_CHARS = 700
_BASIS_WINDOW = 900  # words after the start of a match searched for the basis name
_FORMAT_PDF = "pdf"


@dataclass(frozen=True)
class NoteMatch:
    """Where a filing says its comparatives were restated, and the words it uses."""

    filing_id: str
    page: int  # 1-based
    pattern: str
    text: str

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form (review payloads and event details)."""
        return {
            "filing_id": self.filing_id,
            "page": self.page,
            "pattern": self.pattern,
            "text": self.text,
        }


def _squash(text: str) -> str:
    """Whitespace (line breaks, runs of spaces) collapsed to single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def find_note(
    filing_id: str, patterns: list[str], basis: str | None = None, engine: Engine | None = None
) -> NoteMatch | None:
    """The restatement sentence of a native-text PDF filing -> where it is and what it says.

    A results PDF can hold both statements, each with its own notes. With a `basis`, a sentence
    that names that basis (within the words around the match) is preferred; otherwise the first
    match in page order is returned.
    """
    if not patterns:
        return None
    with transaction(engine) as conn:
        row = conn.execute(
            sa.select(tables.filings.c.raw_path, tables.filings.c.format).where(
                tables.filings.c.filing_id == filing_id
            )
        ).first()
    if (
        row is None
        or row.format != _FORMAT_PDF
        or not row.raw_path
        or not Path(row.raw_path).is_file()
    ):
        return None
    compiled = [(p, re.compile(p, re.IGNORECASE)) for p in patterns]
    first: NoteMatch | None = None
    with pymupdf.open(row.raw_path) as doc:
        for number, page in enumerate(doc, start=1):
            text = _squash(page.get_text("text"))
            for pattern, regex in compiled:
                found = regex.search(text)
                if found is None:
                    continue
                start = (
                    text.rfind(". ", 0, found.start()) + 2 if ". " in text[: found.start()] else 0
                )
                end = text.find(". ", found.end())
                end = len(text) if end < 0 else end + 1
                match = NoteMatch(
                    filing_id,
                    number,
                    pattern,
                    text[start : min(end, start + _SNIPPET_CHARS)].strip(),
                )
                if basis is None or basis in text[start : start + _BASIS_WINDOW].casefold():
                    return match
                first = first or match
    return first
