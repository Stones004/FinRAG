"""Plain data types passed between adapters, the runner, the raw store and the registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from finrag.core.types import Basis, FilingFormat, PeriodType


@dataclass(frozen=True)
class FilingCandidate:
    """One filing a source offers, with whatever the source claims about it.

    `identifier` is what the source called the company (an ISIN or an NSE symbol); `isin` is set
    once known. `period_unverified` marks a guessed period that content must confirm.
    """

    identifier: str
    source: str
    filing_type: str
    format: FilingFormat
    isin: str | None = None
    submission: str = "original"  # original | revision
    revision_no: int | None = None  # None: the registry numbers a revision
    basis: Basis = Basis.UNKNOWN
    period_start: date | None = None
    period_end: date | None = None
    period_type: PeriodType | None = None
    filed_on: str | None = None  # ISO date or datetime
    source_url: str | None = None
    local_path: Path | None = None
    download_url: str | None = None
    period_unverified: bool = False

    @property
    def extension(self) -> str:
        """File extension used in the raw store."""
        return "xml" if self.format is FilingFormat.XBRL else "pdf"


@dataclass(frozen=True)
class Rejected:
    """A source file that could not be turned into a candidate."""

    file_name: str
    reason: str


@dataclass(frozen=True)
class Conflict:
    """One disagreement between what a source claims and what the file says."""

    field: str
    claimed: str | None
    found: str | None
    message: str

    def as_dict(self) -> dict[str, str | None]:
        """Conflict -> JSON-ready dict for review payloads."""
        return {
            "field": self.field,
            "claimed": self.claimed,
            "found": self.found,
            "message": self.message,
        }


@dataclass
class IngestReport:
    """Counts for one ISIN (or one inbox pass); `errors` explains failures."""

    found: int = 0
    new: int = 0
    duplicate: int = 0
    failed: int = 0
    quarantined: int = 0
    link_only: int = 0
    errors: list[str] = field(default_factory=list)

    def stats(self) -> dict[str, int]:
        """Counts only, for the jobs table."""
        return {
            "found": self.found,
            "new": self.new,
            "duplicate": self.duplicate,
            "failed": self.failed,
            "quarantined": self.quarantined,
            "link_only": self.link_only,
        }
