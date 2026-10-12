"""Parse an XBRL instance into numeric ParsedFacts and separate non-numeric TextFacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from lxml import etree

from finrag.core.types import Basis
from finrag.ingestion.xbrl_meta import XbrlParseError
from finrag.parsing.models import EXTRACTOR_XBRL, Dimension, ParsedFact, TextFact, XbrlResult

_XBRLI = "http://www.xbrl.org/2003/instance"
_LINK = "http://www.xbrl.org/2003/linkbase"
_XBRLDI = "http://xbrl.org/2006/xbrldi"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_INFINITE = "INF"
# Element namespaces that are structure, not facts.
_STRUCTURE = {_XBRLI, _LINK}


@dataclass(frozen=True)
class _Context:
    """A context's period and dimensions."""

    start: date | None
    end: date | None
    dimensions: tuple[Dimension, ...]


def _q(namespace: str, local: str) -> str:
    """Namespace + local name -> Clark-notation tag."""
    return f"{{{namespace}}}{local}"


def _qname(element: etree._Element) -> str:
    """Element -> 'prefix:local' using the document's prefix (Clark form if it has none)."""
    local = etree.QName(element).localname
    namespace = etree.QName(element).namespace
    if element.prefix:
        return f"{element.prefix}:{local}"
    return f"{{{namespace}}}{local}" if namespace else local


def _date(text: str | None) -> date | None:
    """'YYYY-MM-DD' (possibly with a time) -> date, or None."""
    try:
        return date.fromisoformat(text.strip()[:10]) if text else None
    except ValueError:
        return None


def _dimension(member: etree._Element) -> Dimension:
    """An explicitMember/typedMember element -> Dimension."""
    axis = (member.get("dimension") or "").strip()
    if etree.QName(member).localname == "typedMember":
        child = next((c for c in member if isinstance(c.tag, str)), None)
        text = "".join(child.itertext()).strip() if child is not None else ""
        return Dimension(axis, text, typed=True)
    return Dimension(axis, (member.text or "").strip())


def _read_context(context: etree._Element) -> _Context:
    """A context element -> its period (instants have start == end) and dimensions."""
    period = context.find(_q(_XBRLI, "period"))
    start = end = None
    if period is not None:
        instant = period.find(_q(_XBRLI, "instant"))
        if instant is not None:
            start = end = _date(instant.text)
        else:
            start = _date(period.findtext(_q(_XBRLI, "startDate")) or "")
            end = _date(period.findtext(_q(_XBRLI, "endDate")) or "")
    members = [m for m in context.iter(_q(_XBRLDI, "explicitMember"), _q(_XBRLDI, "typedMember"))]
    return _Context(start, end, tuple(_dimension(m) for m in members))


def _measure_text(measure: etree._Element) -> str:
    """A measure element -> its text ('iso4217:INR')."""
    return (measure.text or "").strip()


def _read_unit(unit: etree._Element) -> str:
    """A unit element -> 'iso4217:INR' or 'iso4217:INR/xbrli:shares'."""
    divide = unit.find(_q(_XBRLI, "divide"))
    if divide is None:
        return "*".join(_measure_text(m) for m in unit.findall(_q(_XBRLI, "measure")))
    top = divide.find(_q(_XBRLI, "unitNumerator"))
    bottom = divide.find(_q(_XBRLI, "unitDenominator"))
    numerator = "*".join(_measure_text(m) for m in top.findall(_q(_XBRLI, "measure")))
    denominator = "*".join(_measure_text(m) for m in bottom.findall(_q(_XBRLI, "measure")))
    return f"{numerator}/{denominator}"


def _source_loc(context_ref: str, concept: str, dimensions: tuple[Dimension, ...]) -> str:
    """contextRef + concept, plus the dimension members for readability."""
    loc = f"{context_ref} {concept}"
    if dimensions:
        loc += " [" + "; ".join(f"{d.axis}={d.member}" for d in dimensions) + "]"
    return loc


def _decimals(text: str | None) -> int | None:
    """decimals attribute -> int, or None for INF / missing."""
    if text is None or text.strip().upper() == _INFINITE:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise XbrlParseError(f"invalid decimals attribute: {text!r}") from exc


def _numeric_value(element: etree._Element, concept: str) -> Decimal | None:
    """A numeric fact element -> Decimal (None for xsi:nil); bad text raises XbrlParseError."""
    if element.get(_XSI_NIL) in ("true", "1"):
        return None
    text = (element.text or "").strip()
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise XbrlParseError(f"{concept}: not a number: {text!r}") from exc


def parse_xbrl(data: bytes, basis: Basis | str = Basis.UNKNOWN) -> XbrlResult:
    """XBRL instance bytes (+ the filing's basis) -> numeric facts and non-numeric text facts.

    Every fact is kept, with its dimensions; values are full units (scale 0).
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise XbrlParseError(f"not valid XML: {exc}") from exc
    contexts = {c.get("id"): _read_context(c) for c in root.iter(_q(_XBRLI, "context"))}
    units = {u.get("id"): _read_unit(u) for u in root.iter(_q(_XBRLI, "unit"))}
    basis_text = Basis(basis).value
    result = XbrlResult()
    for element in root:
        if not isinstance(element.tag, str) or etree.QName(element).namespace in _STRUCTURE:
            continue
        concept = _qname(element)
        context_ref = element.get("contextRef")
        context = contexts.get(context_ref)
        if context is None:
            raise XbrlParseError(f"{concept}: unknown contextRef {context_ref!r}")
        unit_ref = element.get("unitRef")
        if unit_ref is None:
            result.text_facts.append(
                TextFact(
                    concept,
                    context_ref,
                    context.start,
                    context.end,
                    "".join(element.itertext()).strip(),
                    context.dimensions,
                )
            )
            continue
        if unit_ref not in units:
            raise XbrlParseError(f"{concept}: unknown unitRef {unit_ref!r}")
        result.facts.append(
            ParsedFact(
                value=_numeric_value(element, concept),
                concept_raw=concept,
                period_start=context.start,
                period_end=context.end,
                basis=basis_text,
                unit_raw=units[unit_ref],
                scale=0,
                decimals=_decimals(element.get("decimals")),
                source_loc=_source_loc(context_ref, concept, context.dimensions),
                dimensions=context.dimensions,
                extractor=EXTRACTOR_XBRL,
                confidence=1.0,
            )
        )
    return result
