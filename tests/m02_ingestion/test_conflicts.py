from dataclasses import replace
from datetime import date

from finrag.core.types import Basis, FilingFormat, PeriodType
from finrag.ingestion.conflicts import transcript_conflicts, xbrl_conflicts
from finrag.ingestion.models import FilingCandidate
from finrag.ingestion.transcript_check import TranscriptCheck
from finrag.ingestion.xbrl_meta import parse_xbrl_meta


def claim(kit, **kw) -> FilingCandidate:
    base = dict(
        identifier=kit.ALPHA, isin=kit.ALPHA, source="local_inbox", filing_type="financial_results",
        format=FilingFormat.XBRL, basis=Basis.CONSOLIDATED, period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31), period_type=PeriodType.Q,
    )  # fmt: skip
    return FilingCandidate(**{**base, **kw})


def fields(conflicts) -> list[str]:
    return [c.field for c in conflicts]


def test_consistent_file_has_no_conflicts(kit) -> None:
    assert xbrl_conflicts(claim(kit), parse_xbrl_meta(kit.make_xbrl())) == []


def test_netweb_style_half_yearly_original_lists_each_conflict(kit) -> None:
    meta = parse_xbrl_meta(
        kit.make_xbrl(
            isin=kit.ALPHA_TYPO, start="2025-10-01", end="2026-03-31", period_type="Half Yearly"
        )
    )
    found = xbrl_conflicts(claim(kit), meta)
    assert fields(found) == ["isin", "period_start", "period_type"]
    assert "check digit" in found[0].message
    assert found[2].claimed == "Q" and found[2].found == "Half Yearly"
    assert "Q4 FY26" in found[2].message


def test_valid_but_different_isin_is_a_conflict(kit) -> None:
    found = xbrl_conflicts(claim(kit), parse_xbrl_meta(kit.make_xbrl(isin=kit.BETA)))
    assert fields(found) == ["isin"] and found[0].found == kit.BETA


def test_basis_mismatch(kit) -> None:
    found = xbrl_conflicts(claim(kit), parse_xbrl_meta(kit.make_xbrl(basis="Standalone")))
    assert fields(found) == ["basis"]


def test_unknown_claimed_basis_is_not_a_conflict(kit) -> None:
    candidate = claim(kit, basis=Basis.UNKNOWN)
    assert xbrl_conflicts(candidate, parse_xbrl_meta(kit.make_xbrl(basis="Standalone"))) == []


def test_unrecognised_period_type_value_goes_to_review_not_a_crash(kit) -> None:
    found = xbrl_conflicts(claim(kit), parse_xbrl_meta(kit.make_xbrl(period_type="Odd")))
    assert fields(found) == ["period_type_unrecognised"] and found[0].found == "Odd"
    for value in ("Annual", "Nine Months", "Yearly"):
        assert fields(xbrl_conflicts(claim(kit), parse_xbrl_meta(kit.make_xbrl(period_type=value))))


def test_missing_period_type_is_not_a_conflict(kit) -> None:
    meta = parse_xbrl_meta(kit.make_xbrl())
    assert xbrl_conflicts(claim(kit), replace(meta, period_type_raw=None)) == []


def transcript_claim(kit, **kw) -> FilingCandidate:
    return claim(
        kit,
        filing_type="earnings_call_transcript",
        format=FilingFormat.PDF,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        **kw,
    )


def test_transcript_title_quarter_must_match(kit) -> None:
    candidate = transcript_claim(kit)
    assert transcript_conflicts(candidate, TranscriptCheck(False, title_quarter="Q1 FY27")) == []
    found = transcript_conflicts(candidate, TranscriptCheck(False, title_quarter="Q4 FY26"))
    assert fields(found) == ["quarter"] and "Q4 FY26" in found[0].message


def test_no_recognisable_title_is_quarter_unconfirmed_named_file_or_guess(kit) -> None:
    check = TranscriptCheck(False, title_quarter=None)
    assert fields(transcript_conflicts(transcript_claim(kit), check)) == ["quarter_unconfirmed"]
    guessed = replace(transcript_claim(kit), period_unverified=True)
    found = transcript_conflicts(guessed, check)
    assert fields(found) == ["quarter_unconfirmed"] and "guessed" in found[0].message
