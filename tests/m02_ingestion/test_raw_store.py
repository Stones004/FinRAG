import os
import stat
from dataclasses import replace
from datetime import date

import pytest

from finrag.core.types import Basis, FilingFormat, PeriodType
from finrag.ingestion.models import FilingCandidate
from finrag.ingestion.raw_store import RawStoreError, raw_path_for, sha256_of, store_raw


def candidate(kit, **kw) -> FilingCandidate:
    base = dict(
        identifier=kit.ALPHA, isin=kit.ALPHA, source="local_inbox", filing_type="financial_results",
        format=FilingFormat.XBRL, basis=Basis.CONSOLIDATED, period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31), period_type=PeriodType.Q,
    )  # fmt: skip
    return FilingCandidate(**{**base, **kw})


def test_path_is_isin_fiscal_year_filing_id(kit) -> None:
    filing_id, path, sha = store_raw(b"hello", candidate(kit), kit.ALPHA)
    assert filing_id == "FIL-" + sha[:12] == "FIL-" + sha256_of(b"hello")[:12]
    assert path.parts[-3:] == (kit.ALPHA, "FY26", f"{filing_id}.xml")
    assert path.read_bytes() == b"hello"


def test_pdf_extension_and_unknown_year(kit) -> None:
    pdf = candidate(kit, format=FilingFormat.PDF, period_end=None, period_start=None)
    path = raw_path_for(pdf, "FIL-abc", kit.ALPHA)
    assert path.name == "FIL-abc.pdf" and path.parent.name == "unknown"


def test_file_is_read_only(kit) -> None:
    _, path, _ = store_raw(b"data", candidate(kit), kit.ALPHA)
    assert not path.stat().st_mode & stat.S_IWRITE
    with pytest.raises(PermissionError):
        path.write_bytes(b"changed")


def test_storing_the_same_bytes_again_leaves_the_file_alone(kit) -> None:
    _, path, _ = store_raw(b"data", candidate(kit), kit.ALPHA)
    mtime = path.stat().st_mtime_ns
    _, again, _ = store_raw(b"data", candidate(kit), kit.ALPHA)
    assert again == path and path.stat().st_mtime_ns == mtime


def test_existing_file_with_other_content_is_never_overwritten(kit) -> None:
    filing_id, path, _ = store_raw(b"data", candidate(kit), kit.ALPHA)
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    path.write_bytes(b"tampered")  # simulate damage
    os.chmod(path, stat.S_IREAD)
    with pytest.raises(RawStoreError):
        store_raw(b"data", replace(candidate(kit)), kit.ALPHA)
    assert path.read_bytes() == b"tampered" and filing_id in path.name
