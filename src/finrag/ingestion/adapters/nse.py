"""NSE adapter: integrated filings, old-format results, annual reports and transcripts.

Disabled by default (`sources.enabled`). Field names come from the responses recorded in
tests/fixtures/nse/ on 2026-10-12. The parsers are pure functions over decoded JSON.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Protocol

from finrag.core.config import Settings, get_settings
from finrag.core.fiscal import fiscal_year_of, fy_label, label_to_period
from finrag.core.logging import get_logger
from finrag.core.types import Basis, FilingFormat, PeriodType
from finrag.ingestion.adapters.base import AdapterError, SourceAdapter
from finrag.ingestion.adapters.local_inbox import (
    FILING_TYPE_ANNUAL,
    FILING_TYPE_RESULTS,
    FILING_TYPE_TRANSCRIPT,
)
from finrag.ingestion.http import HttpClient
from finrag.ingestion.models import FilingCandidate

SOURCE = "nse"
_DATE_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y")
_QUERY_DATE = "%d-%m-%Y"
_NULL_SEGMENT = "null"  # NSE writes ".../corporate/null" when an attachment is absent
_MONTHS_TO_TYPE = {3: PeriodType.Q, 6: PeriodType.H, 9: PeriodType.NINE_M, 12: PeriodType.FY}
_QUARTER_MONTHS = 3
_REVISION = "revision"

log = get_logger("ingestion")


def _parse(text: Any) -> datetime | None:
    """'28-Jul-2026 15:32:17' | '30-JUN-2026' -> datetime, anything else -> None."""
    if not isinstance(text, str) or not text.strip():
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def _iso(text: Any) -> str | None:
    """NSE date text -> ISO date (or datetime when a time is present), else None."""
    parsed = _parse(text)
    if parsed is None:
        return None
    return parsed.isoformat(timespec="seconds") if ":" in str(text) else parsed.date().isoformat()


def _basis(text: Any) -> Basis:
    """NSE 'Standalone' | 'Consolidated' | 'Non-Consolidated' -> Basis."""
    lowered = str(text or "").strip().lower()
    if lowered == "consolidated":
        return Basis.CONSOLIDATED
    if lowered in ("standalone", "non-consolidated"):
        return Basis.STANDALONE
    return Basis.UNKNOWN


def _real_url(url: Any) -> str | None:
    """URL or None; an absent attachment shows as '', None or '.../null'."""
    if not isinstance(url, str) or not url.strip():
        return None
    if url.rstrip("/").rsplit("/", 1)[-1].lower() == _NULL_SEGMENT:
        return None
    return url.strip()


def quarter_start(end: date) -> date:
    """Quarter-end date -> first day of that quarter."""
    index = end.year * 12 + end.month - 1 - (_QUARTER_MONTHS - 1)
    return date(index // 12, index % 12 + 1, 1)


def last_completed_quarter(on: date, settings: Settings | None = None) -> tuple[date, date]:
    """Date -> (start, end) of the latest fiscal quarter that ended before that date."""
    settings = settings or get_settings()
    year = fiscal_year_of(on, settings.fiscal)
    best = None
    for fy in (year - 1, year):
        for number in range(1, 5):
            period = label_to_period(f"Q{number} {fy_label(fy, settings.fiscal)}", settings.fiscal)
            if period.end < on and (best is None or period.end > best.end):
                best = period
    if best is None:
        raise ValueError(f"no fiscal quarter ends before {on}")
    return best.start, best.end


def parse_integrated(rows: list[dict], isin: str, symbol: str) -> list[FilingCandidate]:
    """Integrated-filing rows -> XBRL candidates (and a PDF candidate when one is attached)."""
    out: list[FilingCandidate] = []
    for row in rows:
        end = _parse(row.get("qe_Date"))
        if end is None:
            log.warning("integrated row without qe_Date skipped", extra={"event": "skip_row"})
            continue
        revision = str(row.get("type_Sub", "")).strip().lower() == _REVISION
        filed_on = (
            _iso(row.get("revised_Date") if revision else row.get("broadcast_Date"))
            or _iso(row.get("broadcast_Date"))
            or _iso(row.get("creation_Date"))
        )
        for field_name, fmt in (("xbrl", FilingFormat.XBRL), ("pdf_attach", FilingFormat.PDF)):
            url = _real_url(row.get(field_name))
            if url is None:
                continue
            out.append(
                FilingCandidate(
                    identifier=symbol,
                    isin=isin,
                    source=SOURCE,
                    filing_type=FILING_TYPE_RESULTS,
                    format=fmt,
                    submission=_REVISION if revision else "original",
                    basis=_basis(row.get("consolidated")),
                    period_start=quarter_start(end.date()),
                    period_end=end.date(),
                    period_type=PeriodType.Q,
                    filed_on=filed_on,
                    source_url=url,
                    download_url=url,
                )
            )
    return out


def parse_legacy(
    rows: list[dict], isin: str, symbol: str, before: date | None = None
) -> list[FilingCandidate]:
    """Old-format results rows -> XBRL candidates; the row's own `isin` is not trusted."""
    out: list[FilingCandidate] = []
    for row in rows:
        start, end = _parse(row.get("fromDate")), _parse(row.get("toDate"))
        url = _real_url(row.get("xbrl"))
        if start is None or end is None or url is None:
            continue
        if before is not None and end.date() >= before:
            continue  # the integrated filing covers this period
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        out.append(
            FilingCandidate(
                identifier=symbol,
                isin=isin,
                source=SOURCE,
                filing_type=FILING_TYPE_RESULTS,
                format=FilingFormat.XBRL,
                basis=_basis(row.get("consolidated")),
                period_start=start.date(),
                period_end=end.date(),
                period_type=_MONTHS_TO_TYPE.get(months),
                filed_on=_iso(row.get("broadCastDate")) or _iso(row.get("exchdisstime")),
                source_url=url,
                download_url=url,
            )
        )
    return out


def parse_annual_reports(rows: list[dict], isin: str, symbol: str) -> list[FilingCandidate]:
    """Annual-report rows -> PDF candidates; the fiscal year is `toYr`."""
    out: list[FilingCandidate] = []
    for row in rows:
        url = _real_url(row.get("fileName"))
        try:
            period = label_to_period(fy_label(int(row["toYr"])))
        except (KeyError, ValueError, TypeError):
            log.warning("annual report row without a usable toYr skipped", extra={"event": "skip"})
            continue
        if url is None:
            continue
        out.append(
            FilingCandidate(
                identifier=symbol,
                isin=isin,
                source=SOURCE,
                filing_type=FILING_TYPE_ANNUAL,
                format=FilingFormat.PDF,
                submission="original"
                if str(row.get("submission_type", "")).strip().lower() == "new"
                else _REVISION,
                period_start=period.start,
                period_end=period.end,
                period_type=period.type,
                filed_on=_iso(row.get("broadcast_dttm")),
                source_url=url,
                download_url=url,
            )
        )
    return out


def parse_transcripts(
    rows: list[dict], isin: str, symbol: str, settings: Settings | None = None
) -> list[FilingCandidate]:
    """Announcement rows -> transcript candidates (period guessed, to be confirmed from content)."""
    settings = settings or get_settings()
    nse = settings.sources.nse
    marker = nse.transcript_text_marker.lower()
    out: list[FilingCandidate] = []
    for row in rows:
        if row.get("desc") != nse.transcript_desc:
            continue
        if marker not in str(row.get("attchmntText", "")).lower():
            continue
        url = _real_url(row.get("attchmntFile"))
        announced = _parse(row.get("an_dt"))
        if url is None or announced is None:
            continue
        start, end = last_completed_quarter(announced.date(), settings)
        out.append(
            FilingCandidate(
                identifier=symbol,
                isin=isin,
                source=SOURCE,
                filing_type=FILING_TYPE_TRANSCRIPT,
                format=FilingFormat.PDF,
                period_start=start,
                period_end=end,
                period_type=PeriodType.Q,
                filed_on=_iso(row.get("an_dt")),
                source_url=url,
                download_url=url,
                period_unverified=True,
            )
        )
    return out


def _rows(payload: Any) -> list[dict]:
    """A decoded response -> its list of records (a bare list, or {'data': [...]})."""
    if isinstance(payload, dict):
        payload = payload.get("data", [])
    return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []


class SymbolLookup(Protocol):
    """What the NSE adapter needs from the entity registry."""

    def symbol_for(self, isin: str) -> str | None:
        """ISIN -> NSE symbol, or None."""


class NseAdapter(SourceAdapter):
    """Lists and downloads NSE filings through an injected HttpClient."""

    name = SOURCE

    def __init__(
        self,
        settings: Settings | None,
        http: HttpClient,
        lookup: SymbolLookup,
        today: date | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._http = http
        self._lookup = lookup
        self._today = today

    def _symbol(self, isin: str) -> str:
        """ISIN -> NSE symbol, or AdapterError."""
        symbol = self._lookup.symbol_for(isin)
        if not symbol:
            raise AdapterError(f"no NSE symbol known for {isin}")
        return symbol

    def _windows(self, since: date, until: date) -> list[tuple[date, date]]:
        """Split [since, until] into ranges of at most announcement_window_days."""
        span = timedelta(days=self._settings.sources.nse.announcement_window_days - 1)
        windows, start = [], since
        while start <= until:
            end = min(start + span, until)
            windows.append((start, end))
            start = end + timedelta(days=1)
        return windows

    def list_filings(self, isin: str, since: date | None) -> list[FilingCandidate]:
        """ISIN -> candidates from all four NSE listings."""
        nse = self._settings.sources.nse
        symbol = self._symbol(isin)
        until = self._today or date.today()
        since = since or date(until.year - self._settings.history.years_back, until.month, 1)
        base = {"index": "equities", "symbol": symbol}
        out = parse_integrated(
            _rows(
                self._http.get_json(
                    nse.endpoints.integrated_filing_results,
                    {
                        **base,
                        "type": nse.integrated_filing_type,
                        "from_date": since.strftime(_QUERY_DATE),
                        "to_date": until.strftime(_QUERY_DATE),
                    },
                )
            ),
            isin,
            symbol,
        )
        for period in nse.legacy_result_periods:
            rows = _rows(
                self._http.get_json(
                    nse.endpoints.legacy_financial_results, {**base, "period": period}
                )
            )
            out += parse_legacy(rows, isin, symbol, nse.integrated_from_period_end)
        out += parse_annual_reports(
            _rows(self._http.get_json(nse.endpoints.annual_reports, base)), isin, symbol
        )
        for start, end in self._windows(since, until):
            rows = _rows(
                self._http.get_json(
                    nse.endpoints.corporate_announcements,
                    {
                        **base,
                        "from_date": start.strftime(_QUERY_DATE),
                        "to_date": end.strftime(_QUERY_DATE),
                    },
                )
            )
            out += parse_transcripts(rows, isin, symbol, self._settings)
        unique = {c.source_url: c for c in out}  # overlapping windows can repeat a record
        return [
            c for c in unique.values() if not c.filed_on or c.filed_on[:10] >= since.isoformat()
        ]

    def fetch(self, candidate: FilingCandidate) -> bytes:
        """Candidate -> downloaded bytes."""
        if not candidate.download_url:
            raise AdapterError("candidate has no download_url")
        return self._http.get_bytes(candidate.download_url)
