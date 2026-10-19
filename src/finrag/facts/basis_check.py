"""PDF/OCR columns filed under the wrong basis (M05).

A results PDF can hold the standalone and the consolidated statements. A page whose heading names
no basis takes the filing's basis, which is wrong for half of such a PDF (Gabriel's Q1 FY27 PDF:
the standalone P&L on page 4 carries no basis word). The page cannot say, but the numbers can:
if a PDF column of one basis agrees (within the PDF's rounding) with the XBRL values of the
*other* basis on every key it shares with it - and at least `cross_source.min_shifted_keys` of
them - and disagrees with its own basis, it was labelled with the wrong basis.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal

from finrag.core.config import Settings
from finrag.core.types import SourceClass
from finrag.facts.current_view import best
from finrag.facts.models import Fact

UNIT_INR = "INR"
PDF_CLASSES = (SourceClass.PDF, SourceClass.OCR)
Key = tuple[str, date, date]  # concept, start, end
Group = Hashable  # what one "column" is: by default (pdf filing, period end)


def beyond_rounding(xbrl: Fact, other: Fact, settings: Settings) -> bool:
    """True if two values differ by more than the PDF's own rounding."""
    diff = abs(other.value - xbrl.value)
    if xbrl.unit == UNIT_INR:
        return diff > Decimal(settings.validation.cross_source.rounding_inr)
    larger = max(abs(xbrl.value), abs(other.value))
    return larger != 0 and diff / larger > Decimal(str(settings.validation.relative_tolerance))


def by_key(facts: Iterable[Fact]) -> dict[Key, Fact]:
    """Versions -> the best one per (concept, start, end)."""
    groups: dict[Key, list[Fact]] = defaultdict(list)
    for fact in facts:
        groups[(fact.concept, fact.period.start, fact.period.end)].append(fact)
    return {key: top for key, versions in groups.items() if (top := best(versions))}


def _agreement(
    pdf: Mapping[Key, Fact], xbrl: Mapping[Key, Fact], settings: Settings
) -> tuple[int, int]:
    """(keys compared, keys within rounding) of PDF values against one basis's XBRL values."""
    compared = matching = 0
    for key, fact in pdf.items():
        reference = xbrl.get(key)
        if reference is None or reference.unit != fact.unit:
            continue
        compared += 1
        matching += not beyond_rounding(reference, fact, settings)
    return compared, matching


def default_group(fact: Fact) -> Group:
    """A column is the facts of one PDF filing for one period end."""
    return (fact.source_filing_id, fact.period.end)


def misbased_columns(
    versions_by_basis: Mapping[str, Sequence[Fact]],
    settings: Settings,
    group_of: Callable[[Fact], Group] = default_group,
) -> dict[tuple[Group, str], str]:
    """(column, labelled basis) -> the basis its values actually match.

    Only columns that disagree with their own basis's XBRL and agree on every shared key with
    exactly one other basis (at least `min_shifted_keys` keys) are returned. `group_of` says what
    one column is (the gate narrows it to one PDF page).
    """
    xbrl = {
        basis: by_key(f for f in facts if f.source_class is SourceClass.XBRL)
        for basis, facts in versions_by_basis.items()
    }
    minimum = settings.validation.cross_source.min_shifted_keys
    found: dict[tuple[Group, str], str] = {}
    for basis, facts in versions_by_basis.items():
        columns: dict[Group, list[Fact]] = defaultdict(list)
        for fact in facts:
            if fact.source_class in PDF_CLASSES:
                columns[group_of(fact)].append(fact)
        for group, members in columns.items():
            pdf = by_key(members)
            own_compared, own_matching = _agreement(pdf, xbrl.get(basis, {}), settings)
            if own_matching >= own_compared:  # agrees with its own basis (or nothing to compare)
                continue
            candidates = []
            for other, reference in xbrl.items():
                if other == basis:
                    continue
                compared, matching = _agreement(pdf, reference, settings)
                if compared >= minimum and matching == compared:
                    candidates.append(other)
            if len(candidates) == 1:
                found[(group, basis)] = candidates[0]
    return found
