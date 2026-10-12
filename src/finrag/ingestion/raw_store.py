"""Immutable raw file store: write once, read-only, never overwritten."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from finrag.core.config import Settings, get_settings
from finrag.core.fiscal import fiscal_year_of, fy_label
from finrag.core.ids import filing_id_from_sha
from finrag.ingestion.models import FilingCandidate

UNKNOWN_YEAR = "unknown"


class RawStoreError(Exception):
    """Raised when a raw file would be overwritten with different content."""


def sha256_of(data: bytes) -> str:
    """Bytes -> hex sha256."""
    return hashlib.sha256(data).hexdigest()


def raw_path_for(
    candidate: FilingCandidate, filing_id: str, isin: str, settings: Settings | None = None
) -> Path:
    """Candidate -> raw/<ISIN>/<fiscal year label>/<filing_id>.<ext>."""
    settings = settings or get_settings()
    year = (
        fy_label(fiscal_year_of(candidate.period_end, settings.fiscal), settings.fiscal)
        if candidate.period_end
        else UNKNOWN_YEAR
    )
    return settings.paths.raw_store / isin / year / f"{filing_id}.{candidate.extension}"


def store_raw(
    data: bytes, candidate: FilingCandidate, isin: str, settings: Settings | None = None
) -> tuple[str, Path, str]:
    """Bytes + candidate -> (filing_id, raw_path, sha256); writes once and sets read-only.

    If the identical file is already there it is left alone; different content at the same path
    (which cannot happen with a sha-based name) raises RawStoreError.
    """
    sha = sha256_of(data)
    filing_id = filing_id_from_sha(sha)
    path = raw_path_for(candidate, filing_id, isin, settings)
    if path.exists():
        if sha256_of(path.read_bytes()) != sha:
            raise RawStoreError(f"{path} exists with different content; refusing to overwrite")
        return filing_id, path, sha
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
    os.chmod(path, stat.S_IREAD)
    return filing_id, path, sha
