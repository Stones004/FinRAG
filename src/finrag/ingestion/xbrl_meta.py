"""Read filing-level metadata (ISIN, period, basis, ...) from an XBRL instance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from lxml import etree

from finrag.core.types import Basis, PeriodType, is_valid_isin

# Element local names in the in-capmkt namespace (PROGRESS finding 5, checked on real files).
_FIELDS = {
    "isin": "ISIN",
    "symbol": "Symbol",
    "scrip_code": "ScripCode",
    "name": "NameOfTheCompany",
    "nature": "NatureOfReportStandaloneConsolidated",
    "period_start": "DateOfStartOfReportingPeriod",
    "period_end": "DateOfEndOfReportingPeriod",
    "period_type_raw": "TypeOfReportingPeriod",
    "reporting_quarter": "ReportingQuarter",
    "audited": "WhetherResultsAreAuditedOrUnaudited",
    "level_of_rounding": "LevelOfRounding",
    "board_meeting": "DateOfBoardMeetingWhenFinancialResultsWereApproved",
}
_NAMESPACE_SUFFIX = "/in-capmkt"
_ENT_PREFIX = "in-capmkt-ent"
# Words in the entity namespace that name the taxonomy family (…/IntegratedFinance_IndAS/…).
_FAMILY_WORDS = {"indas": "indas", "banking": "banking"}
# TypeOfReportingPeriod values seen so far. TODO: add others when seen; unknown values stay
# uninterpreted and the period dates are checked instead.
_TYPE_BY_RAW = {"quarterly": PeriodType.Q, "half yearly": PeriodType.H}
_MONTHS_TO_TYPE = {3: PeriodType.Q, 6: PeriodType.H, 9: PeriodType.NINE_M, 12: PeriodType.FY}


class XbrlParseError(Exception):
    """Raised when the bytes are not a readable XBRL instance."""


@dataclass(frozen=True)
class XbrlMeta:
    """Filing-level facts read from the instance; missing elements are None."""

    isin: str | None = None
    symbol: str | None = None
    scrip_code: str | None = None
    name: str | None = None
    basis: Basis | None = None
    period_start: date | None = None
    period_end: date | None = None
    period_type_raw: str | None = None
    reporting_quarter: str | None = None
    audited: str | None = None
    level_of_rounding: str | None = None
    board_meeting_date: date | None = None
    taxonomy_family: str | None = None
    raw_fields: dict[str, str] = field(default_factory=dict)

    @property
    def isin_valid(self) -> bool | None:
        """True/False if an ISIN is present (check digit), None if absent."""
        return None if self.isin is None else is_valid_isin(self.isin)

    @property
    def declared_period_type(self) -> PeriodType | None:
        """The period type the filer declared, if it is a value we recognise."""
        return _TYPE_BY_RAW.get((self.period_type_raw or "").strip().lower())

    @property
    def span_period_type(self) -> PeriodType | None:
        """The period type implied by the start/end dates (3/6/9/12 months), if any."""
        if not (self.period_start and self.period_end):
            return None
        months = (self.period_end.year - self.period_start.year) * 12
        months += self.period_end.month - self.period_start.month + 1
        return _MONTHS_TO_TYPE.get(months)


def _parse_date(text: str | None) -> date | None:
    """'YYYY-MM-DD' -> date, anything else -> None."""
    try:
        return date.fromisoformat(text.strip()) if text else None
    except ValueError:
        return None


def _basis(text: str | None) -> Basis | None:
    """'Standalone' | 'Consolidated' -> Basis, anything else -> None."""
    try:
        return Basis((text or "").strip().lower())
    except ValueError:
        return None


def _family(root: etree._Element) -> str | None:
    """Root namespaces -> 'indas' | 'banking' | 'other', or None without an entity namespace."""
    uri = root.nsmap.get(_ENT_PREFIX)
    if not uri:
        return None
    lowered = uri.lower()
    return next((fam for word, fam in _FAMILY_WORDS.items() if word in lowered), "other")


def parse_xbrl_meta(data: bytes) -> XbrlMeta:
    """XBRL instance bytes -> XbrlMeta (first occurrence of each metadata element)."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise XbrlParseError(f"not valid XML: {exc}") from exc
    wanted = {local: key for key, local in _FIELDS.items()}
    found: dict[str, str] = {}
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        namespace, _, local = element.tag.rpartition("}")
        key = wanted.get(local)
        if key and key not in found and namespace.lstrip("{").endswith(_NAMESPACE_SUFFIX):
            found[key] = (element.text or "").strip()
    return XbrlMeta(
        isin=(found.get("isin") or None) and found["isin"].upper(),
        symbol=found.get("symbol") or None,
        scrip_code=found.get("scrip_code") or None,
        name=found.get("name") or None,
        basis=_basis(found.get("nature")),
        period_start=_parse_date(found.get("period_start")),
        period_end=_parse_date(found.get("period_end")),
        period_type_raw=found.get("period_type_raw") or None,
        reporting_quarter=found.get("reporting_quarter") or None,
        audited=found.get("audited") or None,
        level_of_rounding=found.get("level_of_rounding") or None,
        board_meeting_date=_parse_date(found.get("board_meeting")),
        taxonomy_family=_family(root),
        raw_fields=found,
    )
