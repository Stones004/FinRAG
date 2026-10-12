"""The adapter interface. Adapters never write to the database or to disk."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from finrag.ingestion.models import FilingCandidate, Rejected


class AdapterError(Exception):
    """Raised when a source cannot be queried; the message says what to fix."""


class SourceAdapter(ABC):
    """A place filings come from."""

    name: str

    def begin_run(self) -> None:  # noqa: B027 - optional hook
        """Called once before a run; adapters that cache a listing refresh it here."""

    @abstractmethod
    def list_filings(self, isin: str, since: date | None) -> list[FilingCandidate]:
        """ISIN (+ earliest date) -> the filings the source offers for it."""

    @abstractmethod
    def fetch(self, candidate: FilingCandidate) -> bytes:
        """Candidate -> the file's bytes."""

    def rejected(self) -> list[Rejected]:
        """Items the source could not interpret in the last run (default: none)."""
        return []
