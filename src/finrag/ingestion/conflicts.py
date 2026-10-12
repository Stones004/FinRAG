"""Compare what a source claims about a filing with what the file itself says."""

from __future__ import annotations

from finrag.core.fiscal import period_to_label
from finrag.core.types import Basis, Period
from finrag.ingestion.models import Conflict, FilingCandidate
from finrag.ingestion.transcript_check import TranscriptCheck
from finrag.ingestion.xbrl_meta import XbrlMeta


def _claimed_label(candidate: FilingCandidate) -> str | None:
    """Candidate period -> fiscal label such as 'Q4 FY26', or None if not expressible."""
    if not (candidate.period_start and candidate.period_end and candidate.period_type):
        return None
    try:
        return period_to_label(
            Period(candidate.period_start, candidate.period_end, candidate.period_type)
        )
    except ValueError:
        return None


def xbrl_conflicts(candidate: FilingCandidate, meta: XbrlMeta) -> list[Conflict]:
    """Candidate claims vs XBRL content -> every disagreement (empty if consistent)."""
    found: list[Conflict] = []
    claimed_isin = candidate.isin or candidate.identifier
    if meta.isin_valid is False:
        found.append(
            Conflict(
                "isin",
                claimed_isin,
                meta.isin,
                f"ISIN {meta.isin} in the file fails the check digit",
            )
        )
    elif meta.isin and candidate.isin and meta.isin != candidate.isin.upper():
        found.append(
            Conflict("isin", candidate.isin, meta.isin, "ISIN in the file differs from the claimed")
        )
    for name, claimed, content in (
        ("period_start", candidate.period_start, meta.period_start),
        ("period_end", candidate.period_end, meta.period_end),
    ):
        if claimed and content and claimed != content:
            found.append(
                Conflict(
                    name,
                    claimed.isoformat(),
                    content.isoformat(),
                    f"{name.replace('_', ' ')} is {content} in the file, {claimed} claimed",
                )
            )
    declared = meta.declared_period_type
    if meta.period_type_raw and declared is None:
        found.append(
            Conflict(
                "period_type_unrecognised",
                candidate.period_type.value if candidate.period_type else None,
                meta.period_type_raw,
                f"file declares period type '{meta.period_type_raw}', which is not one of the "
                "known values (Quarterly, Half Yearly); needs a human look",
            )
        )
    if declared and candidate.period_type and declared != candidate.period_type:
        claimed_label = _claimed_label(candidate) or candidate.period_type.value
        found.append(
            Conflict(
                "period_type",
                candidate.period_type.value,
                meta.period_type_raw,
                f"file says period type '{meta.period_type_raw}', but the claim is {claimed_label}",
            )
        )
    if (
        candidate.basis is not Basis.UNKNOWN
        and meta.basis
        and meta.basis is not Basis.UNKNOWN
        and meta.basis != candidate.basis
    ):
        found.append(
            Conflict(
                "basis",
                candidate.basis.value,
                meta.basis.value,
                f"file is {meta.basis.value}, {candidate.basis.value} claimed",
            )
        )
    return found


def transcript_conflicts(candidate: FilingCandidate, check: TranscriptCheck) -> list[Conflict]:
    """Candidate quarter vs the quarter in the transcript's title line."""
    claimed = _claimed_label(candidate)
    if check.title_quarter is None:
        guess = " (guessed from the announcement date)" if candidate.period_unverified else ""
        return [
            Conflict(
                "quarter_unconfirmed",
                claimed,
                None,
                f"no recognisable 'Qn FYyy' title in the transcript to confirm {claimed}{guess}",
            )
        ]
    if claimed and claimed != check.title_quarter:
        return [
            Conflict(
                "quarter",
                claimed,
                check.title_quarter,
                f"transcript title says {check.title_quarter}, {claimed} claimed",
            )
        ]
    return []
