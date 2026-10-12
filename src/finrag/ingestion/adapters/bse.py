"""BSE adapter: skeleton only. The BSE endpoints are not known yet (do not guess them)."""

from __future__ import annotations

from datetime import date

from finrag.ingestion.adapters.base import AdapterError, SourceAdapter
from finrag.ingestion.models import FilingCandidate

SOURCE = "bse"
_NOT_READY = "the BSE adapter is a skeleton: its endpoints are not known yet"


class BseAdapter(SourceAdapter):
    """Placeholder that fails clearly if someone enables it."""

    name = SOURCE

    def list_filings(self, isin: str, since: date | None) -> list[FilingCandidate]:
        """Not implemented."""
        raise AdapterError(_NOT_READY)

    def fetch(self, candidate: FilingCandidate) -> bytes:
        """Not implemented."""
        raise AdapterError(_NOT_READY)
