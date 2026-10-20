"""cross_source: compare XBRL with PDF/OCR values of the same key, and classify what differs.

A difference is a finding, never a restatement and never a change to the fact store. Every key
whose PDF/OCR value differs from the XBRL value by more than the PDF's own rounding is classified
together with the other differing keys of the same PDF filing and period end:

* `restated_comparative_likely` - several lines shift together and the arithmetic of the PDF
  still holds (income - expenses = profit, ...), with no OCR-error pattern on any line;
* `ocr_error_likely` - a few isolated lines, each with an OCR-error pattern (a decimal point
  gained or lost, one wrong digit, a low-confidence cell) and a broken sum in the PDF;
* `unclear` - anything else. Nothing is guessed.

A cluster of native-text PDF comparatives (a column of an earlier period than the filing's own) is
a corporate event only if the arithmetic of the PDF holds (`restated_comparative_likely`) or the
filing's own note says the comparatives were restated and several lines shifted together with no
OCR-error pattern (`explain`). Any other disagreement with an original goes to the review queue.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from finrag.core.config import Settings
from finrag.core.types import Period, SourceClass
from finrag.facts.basis_check import beyond_rounding as _beyond_rounding
from finrag.facts.basis_check import by_key as _by_key
from finrag.facts.models import Fact
from finrag.validation.checks import UNIT_INR, gap
from finrag.validation.rules import Identity

RESTATED = "restated_comparative_likely"
OCR_ERROR = "ocr_error_likely"
UNCLEAR = "unclear"

SIGNAL_DECIMAL_SHIFT = "decimal_shift"
SIGNAL_DIGIT = "digit_substitution"
SIGNAL_LOW_CONFIDENCE = "low_confidence"

EXPLAINED_BY_IDENTITIES = "identities"
EXPLAINED_BY_NOTE = "note"

PDF_CLASSES = (SourceClass.PDF, SourceClass.OCR)


@dataclass(frozen=True)
class KeyDifference:
    """One key whose PDF/OCR value differs from the XBRL value beyond rounding."""

    xbrl: Fact
    other: Fact
    abs_diff: Decimal
    rel_diff: Decimal | None
    breach: bool  # beyond validation.* tolerances (what makes the rule warn)
    signals: tuple[str, ...]  # OCR-error patterns found on this line

    @property
    def concept(self) -> str:
        """Canonical concept."""
        return self.xbrl.concept

    @property
    def period(self) -> Period:
        """Period of the key."""
        return self.xbrl.period

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "concept": self.concept,
            "period_start": self.period.start.isoformat(),
            "period_end": self.period.end.isoformat(),
            "period_type": self.period.type.value,
            "xbrl": format(self.xbrl.value, "f"),
            "xbrl_filing": self.xbrl.source_filing_id,
            "pdf": format(self.other.value, "f"),
            "pdf_class": self.other.source_class.value,
            "pdf_filing": self.other.source_filing_id,
            "pdf_confidence": self.other.confidence,
            "abs_diff": format(self.abs_diff, "f"),
            "rel_diff": None if self.rel_diff is None else format(self.rel_diff, "f"),
            "beyond_tolerance": self.breach,
            "signals": list(self.signals),
        }


@dataclass(frozen=True)
class IdentityEvidence:
    """An arithmetic identity that includes a differing line, tested on both sources."""

    identity: str
    period_start: date
    period_end: date
    xbrl_holds: bool | None  # None = not all its lines exist in that source
    pdf_holds: bool | None
    pdf_residual: Decimal | None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return {
            "identity": self.identity,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "xbrl_holds": self.xbrl_holds,
            "pdf_holds": self.pdf_holds,
            "pdf_residual": None if self.pdf_residual is None else format(self.pdf_residual, "f"),
        }


@dataclass
class Cluster:
    """Every key compared between one PDF/OCR filing and the XBRL facts, for one period end."""

    basis: str
    pdf_filing_id: str
    period_end: date
    compared: dict[tuple[date, str], int] = field(default_factory=dict)  # (end, type) -> keys
    differences: list[KeyDifference] = field(default_factory=list)
    evidence: list[IdentityEvidence] = field(default_factory=list)
    classification: str | None = None
    reasons: list[str] = field(default_factory=list)
    comparative: bool = False  # native-text PDF columns of an earlier period than the filing's
    explained_by: list[str] = field(default_factory=list)  # "identities" and/or "note"
    note: dict[str, Any] | None = None  # the filing's restatement note (page, words), if used

    @property
    def breaches(self) -> list[KeyDifference]:
        """Differences beyond the validation tolerances."""
        return [d for d in self.differences if d.breach]

    def summary(self) -> dict[str, Any]:
        """Classification and evidence for details_json."""
        return {
            "pdf_filing": self.pdf_filing_id,
            "period_end": self.period_end.isoformat(),
            "basis": self.basis,
            "classification": self.classification,
            "reasons": self.reasons,
            "differing_keys": len(self.differences),
            "comparative": self.comparative,
            "explained_by": self.explained_by,
            "periods_affected": sorted({d.period.type.value for d in self.differences}),
            "identities": [e.as_dict() for e in self.evidence],
        }


# --- OCR-error patterns ------------------------------------------------------------------------


def decimal_shift(a: Decimal, b: Decimal, max_power: int) -> bool:
    """True if one value is the other times 10^k, 1 <= k <= max_power (a decimal point slip)."""
    if a == 0 or b == 0 or abs(a) == abs(b):
        return False
    return any(
        abs(a) == abs(b) * 10**k or abs(b) == abs(a) * 10**k for k in range(1, max_power + 1)
    )


def digit_substitution(a: Decimal, b: Decimal, unit: Decimal) -> bool:
    """True if the figures (in `unit` steps) differ in one digit, or by one adjacent swap."""
    if unit <= 0:
        return False
    x = str(int((abs(a) / unit).to_integral_value()))
    y = str(int((abs(b) / unit).to_integral_value()))
    if len(x) != len(y) or x == y:
        return False
    where = [i for i, (p, q) in enumerate(zip(x, y, strict=True)) if p != q]
    if len(where) == 1:
        return True
    return (
        len(where) == 2
        and where[1] == where[0] + 1
        and x[where[0]] == y[where[1]]
        and (x[where[1]] == y[where[0]])
    )


def _signals(xbrl: Fact, other: Fact, settings: Settings) -> tuple[str, ...]:
    """The OCR-error patterns one differing line shows."""
    found: list[str] = []
    cross = settings.validation.cross_source
    if xbrl.unit == UNIT_INR:
        if decimal_shift(xbrl.value, other.value, cross.decimal_shift_max_power):
            found.append(SIGNAL_DECIMAL_SHIFT)
        elif digit_substitution(xbrl.value, other.value, Decimal(cross.rounding_inr)):
            found.append(SIGNAL_DIGIT)
    limit = settings.parsing.pdf.format_outlier_confidence
    if other.confidence is not None and other.confidence <= limit:
        found.append(SIGNAL_LOW_CONFIDENCE)
    return tuple(found)


# --- comparison --------------------------------------------------------------------------------


def _holds(
    identity: Identity, values: dict[str, Decimal], rounding: int
) -> tuple[bool | None, Decimal | None]:
    """Identity on one source's values -> (holds, residual); None when a required line is absent."""
    required = [*identity.parts, *identity.minus, identity.total]
    if any(name not in values for name in required):
        return None, None
    optional = [n for n in identity.optional_parts if n in values]
    computed = sum((values[n] for n in [*identity.parts, *optional]), Decimal(0))
    computed -= sum((values[n] for n in identity.minus), Decimal(0))
    residual = values[identity.total] - computed
    return abs(residual) <= rounding * (len(required) + len(optional) + 1), residual


def _evidence(
    cluster: Cluster,
    identities: list[Identity],
    pdf: dict[tuple[str, date, date], Fact],
    xbrl: dict[tuple[str, date, date], Fact],
    rounding: int,
) -> list[IdentityEvidence]:
    """Identities that include a differing line, tested on the PDF and on the XBRL values."""
    touched: dict[tuple[date, date], set[str]] = defaultdict(set)
    for d in cluster.differences:
        touched[(d.period.start, d.period.end)].add(d.concept)
    found: list[IdentityEvidence] = []
    for (start, end), concepts in sorted(touched.items()):
        pdf_values = {c: f.value for (c, s, e), f in pdf.items() if (s, e) == (start, end)}
        xbrl_values = {c: f.value for (c, s, e), f in xbrl.items() if (s, e) == (start, end)}
        for identity in identities:
            if not concepts & set(identity.concepts()):
                continue
            x_holds, _ = _holds(identity, xbrl_values, rounding)
            p_holds, residual = _holds(identity, pdf_values, rounding)
            found.append(IdentityEvidence(identity.id, start, end, x_holds, p_holds, residual))
    return found


# --- classification ----------------------------------------------------------------------------


def classify(cluster: Cluster, settings: Settings) -> None:
    """Set `classification` and `reasons` from the cluster's differences and identity evidence."""
    diffs = cluster.differences
    if not diffs:
        return
    minimum = settings.validation.cross_source.min_shifted_keys
    informative = [e for e in cluster.evidence if e.xbrl_holds]  # holds on XBRL: a fair test
    broken = [e for e in informative if e.pdf_holds is False]
    held = [e for e in informative if e.pdf_holds]
    pattern = [d for d in diffs if d.signals]
    reasons: list[str] = []
    if len(diffs) >= minimum:
        slips = [
            d
            for d in diffs
            if SIGNAL_DECIMAL_SHIFT in d.signals or (SIGNAL_LOW_CONFIDENCE in d.signals)
        ]
        if held and not broken and not slips:
            cluster.classification = RESTATED
            reasons.append(f"{len(diffs)} lines differ and shift together")
            reasons.append(f"{len(held)} identities that include them hold in the PDF")
            reasons.append("no decimal-shift or low-confidence cell among them")
        else:
            cluster.classification = UNCLEAR
            reasons.append(f"{len(diffs)} lines differ but the evidence is mixed")
            if broken:
                reasons.append(f"{len(broken)} identities break in the PDF")
            if slips:
                reasons.append(f"{len(slips)} lines show an OCR-error pattern")
            if not held:
                reasons.append("no identity including them could be tested on both sources")
    elif len(pattern) == len(diffs) and not any(e.pdf_holds for e in informative):
        cluster.classification = OCR_ERROR
        found = sorted({s for d in diffs for s in d.signals})
        reasons.append(
            f"{len(diffs)} isolated line(s) with an OCR-error pattern: {', '.join(found)}"
        )
        reasons.append(
            f"{len(broken)} identities that include them break in the PDF"
            if broken
            else "no identity including them could be tested in the PDF"
        )
    else:
        cluster.classification = UNCLEAR
        reasons.append(f"{len(diffs)} isolated line(s) without a clear OCR-error pattern")
        if held:
            reasons.append("identities including them hold in the PDF")
    cluster.reasons = reasons


def explain(cluster: Cluster, note: dict[str, Any] | None, settings: Settings) -> None:
    """Set `explained_by`: what makes the differences of this cluster a restatement.

    Identities that still hold in the PDF (classification `restated_comparative_likely`) explain
    any cluster. The filing's own restatement note explains a cluster of native-text comparatives
    whose differences are several lines shifting together, none of them with an OCR-error pattern;
    one isolated line is not what a restatement note is about.
    """
    cluster.explained_by = []
    if cluster.classification == RESTATED:
        cluster.explained_by.append(EXPLAINED_BY_IDENTITIES)
    minimum = settings.validation.cross_source.min_shifted_keys
    if (
        note is not None
        and cluster.comparative
        and len(cluster.differences) >= minimum
        and not any(d.signals for d in cluster.differences)
    ):
        cluster.explained_by.append(EXPLAINED_BY_NOTE)
        cluster.note = note


def analyse(versions: list[Fact], identities: list[Identity], settings: Settings) -> list[Cluster]:
    """Every stored version of one (isin, basis) -> clusters with differences and classification."""
    xbrl = _by_key(f for f in versions if f.source_class is SourceClass.XBRL)
    per_filing: dict[str, list[Fact]] = defaultdict(list)
    for fact in versions:
        if fact.source_class in PDF_CLASSES:
            per_filing[fact.source_filing_id].append(fact)
    rounding = settings.validation.cross_source.rounding_inr
    tol = settings.validation
    clusters: list[Cluster] = []
    for filing_id, facts in sorted(per_filing.items()):
        pdf = _by_key(facts)
        by_end: dict[date, Cluster] = {}
        for key, other in sorted(pdf.items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][0])):
            reference = xbrl.get(key)
            if reference is None or reference.unit != other.unit:
                continue
            cluster = by_end.setdefault(
                other.period.end,
                Cluster(other.basis, filing_id, other.period.end, comparative=True),
            )
            cluster.comparative = cluster.comparative and other.is_pdf_comparative
            slot = (other.period.end, other.period.type.value)
            cluster.compared[slot] = cluster.compared.get(slot, 0) + 1
            if not _beyond_rounding(reference, other, settings):
                continue
            breach, diff, rel = gap(reference.value, other.value, other.unit, tol)
            cluster.differences.append(
                KeyDifference(
                    reference, other, diff, rel, breach, _signals(reference, other, settings)
                )
            )
        for cluster in by_end.values():
            cluster.evidence = _evidence(cluster, identities, pdf, xbrl, rounding)
            classify(cluster, settings)
            clusters.append(cluster)
    return clusters
