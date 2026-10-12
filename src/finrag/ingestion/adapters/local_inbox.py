"""Local inbox adapter: filings the user drops into settings.paths.inbox."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Protocol

from finrag.core.config import Settings, get_settings
from finrag.core.fiscal import label_to_period
from finrag.core.types import Basis, FilingFormat, PeriodType, looks_like_isin
from finrag.ingestion.adapters.base import SourceAdapter
from finrag.ingestion.models import FilingCandidate, Rejected
from finrag.ingestion.xbrl_meta import XbrlParseError, parse_xbrl_meta

SOURCE = "local_inbox"
FILING_TYPE_RESULTS = "financial_results"
FILING_TYPE_ANNUAL = "annual_report"
FILING_TYPE_TRANSCRIPT = "earnings_call_transcript"
_SEPARATOR = "__"
# `rev1`, optionally followed by the exchange's _DDMMYYYYhhmmss_ filing time (see filename_time)
_REVISION_RE = re.compile(r"^rev(?P<n>\d+)(?:_\d{14}_)?$", re.IGNORECASE)
_BASES = {Basis.CONSOLIDATED.value: Basis.CONSOLIDATED, Basis.STANDALONE.value: Basis.STANDALONE}
_FORMAT_BY_SUFFIX = {".xml": FilingFormat.XBRL, ".pdf": FilingFormat.PDF}


class Lookup(Protocol):
    """What the inbox needs from the entity registry."""

    def isin_for(self, identifier: str) -> str | None:
        """Identifier -> ISIN of a known entity, or None."""


class BadName(Exception):
    """Raised when a file name does not follow the convention; the message says why."""


def parse_name(path: Path, settings: Settings) -> FilingCandidate:
    """`<id>__<type>__<period>__<basis>[__rev<N>].<ext>` -> FilingCandidate (raises BadName)."""
    fmt = _FORMAT_BY_SUFFIX.get(path.suffix.lower())
    if fmt is None:
        raise BadName(f"unsupported file type '{path.suffix}' (expected .xml or .pdf)")
    parts = path.stem.split(_SEPARATOR)
    revision_no = None
    if len(parts) == 5 and (match := _REVISION_RE.match(parts[4])):
        revision_no = int(match["n"])
        parts = parts[:4]
    if len(parts) != 4:
        raise BadName("name does not match <id>__<type>__<period>__<basis>[__rev<N>]")
    identifier, filing_type, label, basis_text = parts
    if filing_type not in settings.sources.filing_types:
        raise BadName(f"unknown filing type '{filing_type}'")
    basis = _BASES.get(basis_text.lower())
    if basis is None:
        raise BadName(f"basis '{basis_text}' is not consolidated or standalone")
    try:
        period = label_to_period(label)
    except ValueError as exc:
        raise BadName(str(exc)) from exc
    if filing_type != FILING_TYPE_RESULTS and fmt is not FilingFormat.PDF:
        raise BadName(f"{filing_type} must be a PDF")
    if filing_type == FILING_TYPE_ANNUAL and period.type is not PeriodType.FY:
        raise BadName("an annual report's period must be a fiscal year such as FY26")
    if filing_type == FILING_TYPE_TRANSCRIPT and period.type is not PeriodType.Q:
        raise BadName("a transcript's period must be the quarter discussed, such as Q1FY27")
    return FilingCandidate(
        identifier=identifier,
        isin=identifier.upper() if looks_like_isin(identifier) else None,
        source=SOURCE,
        filing_type=filing_type,
        format=fmt,
        submission="revision" if revision_no is not None else "original",
        revision_no=revision_no,
        basis=basis,
        period_start=period.start,
        period_end=period.end,
        period_type=period.type,
        local_path=path,
    )


def candidate_from_content(path: Path, data: bytes) -> FilingCandidate:
    """An XBRL file with a free-form name -> candidate built from its own content."""
    try:
        meta = parse_xbrl_meta(data)
    except XbrlParseError as exc:
        raise BadName(str(exc)) from exc
    identifier = meta.isin if meta.isin_valid else (meta.symbol or meta.scrip_code or meta.isin)
    if not identifier or not (meta.period_start and meta.period_end) or meta.basis is None:
        raise BadName(
            "file name does not follow the convention and the XBRL lacks ISIN/period/basis"
        )
    return FilingCandidate(
        identifier=identifier,
        isin=identifier if looks_like_isin(identifier) else None,
        source=SOURCE,
        filing_type=FILING_TYPE_RESULTS,
        format=FilingFormat.XBRL,
        basis=meta.basis,
        period_start=meta.period_start,
        period_end=meta.period_end,
        period_type=meta.span_period_type,
        local_path=path,
    )


class LocalInboxAdapter(SourceAdapter):
    """Reads filings from the inbox folder; candidates are cached per run (`begin_run`)."""

    name = SOURCE

    def __init__(self, lookup: Lookup, settings: Settings | None = None) -> None:
        self._lookup = lookup
        self._settings = settings or get_settings()
        self._candidates: list[FilingCandidate] | None = None
        self._rejected: list[Rejected] = []

    def begin_run(self) -> None:
        """Scan the inbox folder again."""
        self._candidates, self._rejected = self._scan()

    def _files(self) -> list[Path]:
        """Files directly in the inbox folder, sorted (the _processed folder is skipped)."""
        inbox = self._settings.paths.inbox
        return sorted(p for p in inbox.iterdir() if p.is_file()) if inbox.is_dir() else []

    def _candidate_for(self, path: Path) -> FilingCandidate:
        """One file -> candidate with a known ISIN (raises BadName)."""
        try:
            candidate = parse_name(path, self._settings)
        except BadName as name_error:
            if path.suffix.lower() != ".xml":
                raise
            try:
                candidate = candidate_from_content(path, path.read_bytes())
            except BadName as content_error:
                raise BadName(f"{name_error}; {content_error}") from content_error
        isin = self._lookup.isin_for(candidate.identifier)
        if isin is None:
            raise BadName(
                f"'{candidate.identifier}' is not a tracked company (run `finrag watchlist sync`)"
            )
        return replace(candidate, isin=isin)

    def _scan(self) -> tuple[list[FilingCandidate], list[Rejected]]:
        """Inbox -> (candidates, rejected files)."""
        found: list[FilingCandidate] = []
        rejected: list[Rejected] = []
        for path in self._files():
            try:
                found.append(self._candidate_for(path))
            except (BadName, OSError) as exc:
                rejected.append(Rejected(path.name, str(exc)))
        return found, rejected

    def all_candidates(self) -> list[FilingCandidate]:
        """Every candidate in the inbox, whatever the ISIN."""
        if self._candidates is None:
            self.begin_run()
        return list(self._candidates or [])

    def list_filings(self, isin: str, since: date | None = None) -> list[FilingCandidate]:
        """Candidates for one ISIN (`since` does not apply to the inbox)."""
        return [c for c in self.all_candidates() if c.isin == isin]

    def fetch(self, candidate: FilingCandidate) -> bytes:
        """Candidate -> the file's bytes."""
        if candidate.local_path is None:
            raise ValueError("inbox candidate has no local_path")
        return candidate.local_path.read_bytes()

    def rejected(self) -> list[Rejected]:
        """Files the last scan could not interpret."""
        self.all_candidates()
        return list(self._rejected)
