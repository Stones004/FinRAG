"""Display formats for values, and the numbers each display makes acceptable to the post-check."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from finrag.core.config import Settings
from finrag.metrics.registry import MetricRegistry
from finrag.normalize.taxonomy import Taxonomy

HUNDRED = Decimal(100)
_NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?!\w)")
_YEAR = re.compile(r"\b(?:19|20)\d{2}(?:-\d{2,4})?\b")
_ALIAS = re.compile(r"\[[^\]]*\]")


@dataclass(frozen=True)
class Rendered:
    """A value as shown, and the number in it (absolute, as displayed) the post-check accepts."""

    text: str
    number: Decimal | None


def quantize(value: Decimal, places: int) -> Decimal:
    """Round half up to `places` decimals."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def numbers_in(text: str) -> list[Decimal]:
    """Text -> the numbers it contains (years, aliases and labels such as FY26 / Q1 excluded)."""
    cleaned = _YEAR.sub(" ", _ALIAS.sub(" ", text))
    out = []
    for token in _NUMBER.findall(cleaned):
        out.append(Decimal(token.replace(",", "")))
    return out


class Formatter:
    """Formats metric values and facts in the configured display unit and decimals."""

    def __init__(self, settings: Settings, registry: MetricRegistry, taxonomy: Taxonomy) -> None:
        self.places = settings.defaults.display_decimals
        self.unit = settings.defaults.display_unit
        self.divisor = Decimal(settings.units.multipliers[self.unit])
        self.registry = registry
        self.taxonomy = taxonomy

    # -- values ------------------------------------------------------------------------------

    def _fmt(self, number: Decimal, suffix: str = "", signed: bool = False) -> str:
        """Number -> '1,234.56' + suffix (with an explicit sign when asked)."""
        shown = quantize(number, self.places)
        sign = "+" if signed and shown > 0 else ""
        return f"{sign}{shown:,.{self.places}f}{suffix}"

    def by_display(self, value: Decimal, display: str) -> Rendered:
        """Value and the registry's display kind -> Rendered."""
        if display == "percent":
            number = value * HUNDRED
            return Rendered(self._fmt(number, "%"), abs(quantize(number, self.places)))
        if display == "percentage_points":
            number = value * HUNDRED
            return Rendered(self._fmt(number, " pp"), abs(quantize(number, self.places)))
        if display == "crore" or display == self.unit:
            number = value / self.divisor
            return Rendered(self._fmt(number, f" {self.unit}"), abs(quantize(number, self.places)))
        if display == "days":
            return Rendered(self._fmt(value, " days"), abs(quantize(value, self.places)))
        if display == "times":
            return Rendered(self._fmt(value, "x"), abs(quantize(value, self.places)))
        return Rendered(self._fmt(value), abs(quantize(value, self.places)))

    def metric(self, value: Decimal, metric_id: str) -> Rendered:
        """A metric value -> Rendered by the metric's display kind."""
        return self.by_display(value, self.registry.get(metric_id).display)

    def concept(self, value: Decimal, concept_id: str) -> Rendered:
        """A fact value -> Rendered by the concept's unit (INR in the display unit)."""
        unit = (
            self.taxonomy.concepts[concept_id].unit
            if concept_id in self.taxonomy.concepts
            else "INR"
        )
        if unit == "RATIO":
            return self.by_display(value, "percent")
        if unit == "INR_PER_SHARE":
            return Rendered(self._fmt(value, " per share"), abs(quantize(value, self.places)))
        if unit == "INR":
            return self.by_display(value, "crore")
        return self.by_display(value, "number")

    def display_of(self, metric_or_concept: str) -> str:
        """ID -> the display kind used for its changes."""
        if metric_or_concept in self.registry:
            return self.registry.get(metric_or_concept).display
        concept = self.taxonomy.concepts.get(metric_or_concept)
        unit = concept.unit if concept else "INR"
        return {"RATIO": "percent", "INR": "crore"}.get(unit, "number")

    def change(self, new: Decimal, old: Decimal, display: str) -> Rendered | None:
        """New and old value -> the change as shown (pp for ratios, % for amounts)."""
        if display in ("percent", "percentage_points"):
            number = (new - old) * HUNDRED
            return Rendered(
                self._fmt(number, " pp", signed=True), abs(quantize(number, self.places))
            )
        if old == 0:
            return None
        number = (new - old) / abs(old) * HUNDRED
        return Rendered(self._fmt(number, "%", signed=True), abs(quantize(number, self.places)))
