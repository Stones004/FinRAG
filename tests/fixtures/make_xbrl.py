"""Build a small synthetic XBRL instance in the exchange layout (no real company data).

Run as a script to write it to disk: `python tests/fixtures/make_xbrl.py out.xml`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

CAP = "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"
ENT = "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/in-capmkt/in-capmkt-ent"

# (concept, contextRef, unitRef, decimals, text): every row is one numeric fact in the file.
NUMERIC = [
    ("RevenueFromOperations", "OneD", "INR", "-4", "7737020000"),
    ("OtherIncome", "OneD", "INR", "-4", "102350000"),
    ("ProfitLossForPeriod", "OneD", "INR", "-4", "-705930000"),
    ("RevenueFromOperations", "FourD", "INR", "-4", "21835630000"),
    ("Assets", "OneI", "INR", "-4", "22549280000"),
    ("BasicEarningsPerShare", "OneD", "INRPerShare", "INF", "12.43"),
    ("PaidUpValueOfEquityShareCapital", "OneI", "INR", "INF", "113880000"),
    ("ShareHoldingPercentage", "OneI", "pure", "2", "0.55"),
    ("OtherExpenses", "OneExpenses1D", "INR", "-4", "398750000"),
    ("RevenueFromOperations", "OneSegD", "INR", "-4", "1000000"),
    ("Remuneration", "AuditorD", "INR", "0", "250000"),
]
# Non-numeric facts (no unitRef): metadata and a text block.
TEXT = [
    ("ISIN", "OneD", "INE111A01010"),
    ("NameOfTheCompany", "OneD", "ALPHA INDUSTRIES LIMITED"),
    ("DescriptionOfOtherExpenses", "OneD", "Freight &amp; packing"),
]


@dataclass(frozen=True)
class Expected:
    """What parsing the fixture must produce."""

    numeric: int = len(NUMERIC)
    text: int = len(TEXT)
    dimensioned: int = 3  # explicit member, typed member (segment), typed member (auditor)
    nil: int = field(default=1)  # one extra nil numeric fact is appended below


def _context(ctx_id: str, period: str, members: str = "") -> str:
    scenario = f"<xbrli:scenario>{members}</xbrli:scenario>" if members else ""
    return (
        f'<xbrli:context id="{ctx_id}"><xbrli:entity>'
        '<xbrli:identifier scheme="http://www.sebi.gov.in/in-capmkt/ScripCode">500001'
        f"</xbrli:identifier></xbrli:entity><xbrli:period>{period}</xbrli:period>{scenario}"
        "</xbrli:context>"
    )


def duration(start: str, end: str) -> str:
    """Start and end dates -> the period element body of a duration context."""
    return f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"


def build_instance() -> bytes:
    """Return the instance as UTF-8 bytes."""
    explicit = (
        '<xbrldi:explicitMember dimension="in-capmkt:DetailsOfOtherExpensesAxis">'
        "in-capmkt:OtherExpenses1Member</xbrldi:explicitMember>"
    )
    typed_segment = (
        '<xbrldi:typedMember dimension="in-capmkt:ReportableSegmentsAxis">'
        "<in-capmkt:ReportableSegmentsDomain>Segment1</in-capmkt:ReportableSegmentsDomain>"
        "</xbrldi:typedMember>"
    )
    typed_auditor = (
        '<xbrldi:typedMember dimension="in-capmkt:AuditorAxis">'
        "<in-capmkt:AuditorDomain>AuditorDomain1</in-capmkt:AuditorDomain></xbrldi:typedMember>"
    )
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!--IFIndAs V2.0 synthetic-->",
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"'
        f' xmlns:in-capmkt="{CAP}" xmlns:in-capmkt-ent="{ENT}"'
        ' xmlns:iso4217="http://www.xbrl.org/2003/iso4217"'
        ' xmlns:link="http://www.xbrl.org/2003/linkbase"'
        ' xmlns:xlink="http://www.w3.org/1999/xlink"'
        ' xmlns:xbrldi="http://xbrl.org/2006/xbrldi"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
        '<link:schemaRef xlink:href="in-capmkt-ent.xsd" xlink:type="simple"/>',
        _context("OneD", duration("2026-01-01", "2026-03-31")),
        _context("FourD", duration("2025-04-01", "2026-03-31")),
        _context("OneI", "<xbrli:instant>2026-03-31</xbrli:instant>"),
        _context("OneExpenses1D", duration("2026-01-01", "2026-03-31"), explicit),
        _context("OneSegD", duration("2026-01-01", "2026-03-31"), typed_segment),
        _context("AuditorD", duration("2026-01-01", "2026-03-31"), typed_auditor),
        '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>',
        '<xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>',
        '<xbrli:unit id="INRPerShare"><xbrli:divide><xbrli:unitNumerator>'
        "<xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator>"
        "<xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide>"
        "</xbrli:unit>",
    ]
    for concept, ctx, unit, decimals, text in NUMERIC:
        parts.append(
            f'<in-capmkt:{concept} contextRef="{ctx}" decimals="{decimals}" unitRef="{unit}">'
            f"{text}</in-capmkt:{concept}>"
        )
    parts.append(
        '<in-capmkt:ExceptionalItems contextRef="OneD" decimals="-4" unitRef="INR" xsi:nil="true"/>'
    )
    for concept, ctx, text in TEXT:
        parts.append(f'<in-capmkt:{concept} contextRef="{ctx}">{text}</in-capmkt:{concept}>')
    parts.append("</xbrli:xbrl>")
    return "\n".join(parts).encode("utf-8")


if __name__ == "__main__":
    with open(sys.argv[1], "wb") as handle:
        handle.write(build_instance())
