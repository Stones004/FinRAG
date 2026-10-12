"""Load and validate the taxonomy registry; build the lookup indexes the mapper uses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from finrag.core.config import load_registry
from finrag.core.types import SectorType

DUPLICATE_PREFIX = "duplicate_of:"
_QNAME = re.compile(r"[A-Za-z][\w.-]*:[A-Za-z_][\w.-]*")
_ENUMERATOR = re.compile(r"^\s*(?:\(?\d+\)?[.)]?|\(?[a-z]\)|\(?[ivx]+\)|[a-z]\.)\s+", re.IGNORECASE)
_PARENTHESES = re.compile(r"\([^)]*\)")
_NON_WORD = re.compile(r"[^0-9a-z]+")
_ROMAN_PREFIX = re.compile(r"^[ivxl]{1,4}\s+(?=[a-z]{3})")  # numbering, often OCR-garbled


class TaxonomyError(Exception):
    """Raised when the taxonomy registry is invalid; the message names every problem found."""


def normalise_label(label: str) -> str:
    """Label as printed -> lowercase words only (no numbering, notes in brackets or punctuation)."""
    text = _ENUMERATOR.sub("", label.casefold().replace("&", " and "))
    text = _PARENTHESES.sub(" ", text)
    return _ROMAN_PREFIX.sub("", _NON_WORD.sub(" ", text).strip())


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Concept(_Model):
    """One canonical concept and everything that maps onto it."""

    label: str
    description: str = ""
    statement: Literal["profit_and_loss", "balance_sheet", "cash_flow", "other"]
    period_kind: Literal["duration", "instant"]
    unit: Literal["INR", "INR_PER_SHARE", "SHARES", "RATIO"]
    sign: Literal["as_reported"] = "as_reported"
    sign_convention: Literal["natural", "outflow_positive"] = "natural"
    # PDF/OCR values are converted to the XBRL convention: negate when the PDF prints the opposite
    pdf_sign: Literal["as_printed", "negate"] = "as_printed"
    # a reported 0 means "not disclosed" (never a real value): such facts are counted, not stored
    zero_is_placeholder: bool = False
    # False: a stock or ratio that M05 must never derive by subtracting cumulative periods
    derivable: bool = True
    sectors: list[SectorType] | None = None  # None = every sector
    xbrl_concepts: dict[str, list[str]] = {}  # taxonomy family -> QNames
    labels: list[str] = []
    opening_concept: str | None = None  # instant before the cash-flow period start -> this concept

    @field_validator("xbrl_concepts")
    @classmethod
    def _qnames(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for names in value.values():
            for name in names:
                if not _QNAME.fullmatch(name):
                    raise ValueError(f"'{name}' is not a prefix:LocalName QName")
        return value


class UnmappedEntry(_Model):
    """A QName that is understood and deliberately not mapped to a canonical concept."""

    reason: str
    note: str = ""


class TaxonomyFile(_Model):
    """Shape of the taxonomy registry."""

    version: int
    families: list[str]
    xbrl_units: dict[str, list[str]]
    unmapped_reasons: dict[str, str]
    concepts: dict[str, Concept]
    known_unmapped: dict[str, dict[str, UnmappedEntry | str]] = {}


@dataclass(frozen=True)
class KnownUnmapped:
    """Lookup result for a known-unmapped QName."""

    qname: str
    reason: str
    duplicate_of: str | None
    note: str


@dataclass
class Taxonomy:
    """Validated taxonomy with the lookup indexes."""

    version: int
    families: tuple[str, ...]
    concepts: dict[str, Concept]
    xbrl_units: dict[str, tuple[str, ...]]
    reasons: dict[str, str]
    by_qname: dict[tuple[str, str], str] = field(default_factory=dict)
    by_label: dict[str, str] = field(default_factory=dict)
    known: dict[tuple[str, str], KnownUnmapped] = field(default_factory=dict)

    @property
    def opening_names(self) -> frozenset[str]:
        """Concepts that hold opening balances (the targets of `opening_concept`)."""
        return frozenset(c.opening_concept for c in self.concepts.values() if c.opening_concept)

    def label_choices(self) -> list[tuple[str, str]]:
        """All (normalised label, concept) pairs, for fuzzy suggestions."""
        return list(self.by_label.items())

    def reason_names(self, family: str) -> set[str]:
        """Reasons used by known-unmapped QNames of a family (for reports)."""
        return {k.reason for (fam, _), k in self.known.items() if fam == family}


def _index(raw: TaxonomyFile) -> tuple[dict, dict, dict, list[str]]:
    """Build the QName, label and known-unmapped indexes; collect every problem found."""
    problems: list[str] = []
    by_qname: dict[tuple[str, str], str] = {}
    by_label: dict[str, str] = {}
    for name, concept in raw.concepts.items():
        for family, qnames in concept.xbrl_concepts.items():
            if family not in raw.families:
                problems.append(f"concept '{name}': unknown taxonomy family '{family}'")
            for qname in qnames:
                other = by_qname.get((family, qname))
                if other is not None:
                    problems.append(
                        f"duplicate mapping: {family} {qname} is on both '{other}' and '{name}'"
                    )
                by_qname[(family, qname)] = name
        for label in concept.labels:
            key = normalise_label(label)
            other = by_label.get(key)
            if other is not None and other != name:
                problems.append(f"duplicate label '{key}' is on both '{other}' and '{name}'")
            by_label[key] = name
        if concept.opening_concept and concept.opening_concept not in raw.concepts:
            problems.append(
                f"concept '{name}': opening_concept '{concept.opening_concept}' unknown"
            )
    known: dict[tuple[str, str], KnownUnmapped] = {}
    for family, entries in raw.known_unmapped.items():
        if family not in raw.families:
            problems.append(f"known_unmapped: unknown taxonomy family '{family}'")
        for qname, entry in entries.items():
            item = UnmappedEntry(reason=entry) if isinstance(entry, str) else entry
            if not _QNAME.fullmatch(qname):
                problems.append(f"known_unmapped: '{qname}' is not a prefix:LocalName QName")
            if (family, qname) in by_qname:
                problems.append(
                    f"{family} {qname} is in known_unmapped and mapped to "
                    f"'{by_qname[(family, qname)]}'"
                )
            duplicate_of = None
            if item.reason.startswith(DUPLICATE_PREFIX):
                duplicate_of = item.reason[len(DUPLICATE_PREFIX) :]
                if duplicate_of not in raw.concepts:
                    problems.append(f"{qname}: duplicate_of unknown concept '{duplicate_of}'")
            elif item.reason not in raw.unmapped_reasons:
                problems.append(f"{qname}: unknown reason '{item.reason}'")
            known[(family, qname)] = KnownUnmapped(qname, item.reason, duplicate_of, item.note)
    return by_qname, by_label, known, problems


def parse_taxonomy(data: dict[str, Any]) -> Taxonomy:
    """Raw registry dict -> validated Taxonomy (TaxonomyError listing every problem)."""
    try:
        raw = TaxonomyFile.model_validate(data)
    except ValidationError as exc:
        lines = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise TaxonomyError("invalid taxonomy registry:\n  " + "\n  ".join(lines)) from exc
    by_qname, by_label, known, problems = _index(raw)
    if problems:
        raise TaxonomyError("invalid taxonomy registry:\n  " + "\n  ".join(problems))
    return Taxonomy(
        raw.version,
        tuple(raw.families),
        raw.concepts,
        {k: tuple(v) for k, v in raw.xbrl_units.items()},
        raw.unmapped_reasons,
        by_qname,
        by_label,
        known,
    )


def load_taxonomy() -> Taxonomy:
    """Read the registry named `taxonomy` in settings -> validated Taxonomy."""
    return parse_taxonomy(load_registry("taxonomy"))
