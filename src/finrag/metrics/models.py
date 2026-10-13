"""Plain types for metric values: a stored value and the report of one run."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from finrag.core.types import MetricStatus, Period

# `details_json["code"]`: why a metric has its status
COMPUTED = "computed"
NOT_APPLICABLE = "not_applicable"
BLOCKED = "blocked_by_validation"
MISSING_INPUT = "missing_input"
EVENT_BREAK = "corporate_event_break"
DIVISION_BY_ZERO = "division_by_zero"


@dataclass(frozen=True)
class MetricValue:
    """One row of metric_values with its details decoded.

    `details` holds the machine-readable parts, which the answer and the UI must show next to the
    number: `basis_change` (events), `basis_fallback`, `assumed_zero`, `warnings`, `restatements`.
    """

    metric_value_id: str
    isin: str
    metric_id: str
    formula_version: str
    period: Period
    basis: str
    value: Decimal | None
    status: MetricStatus
    reason: str
    details: dict[str, Any]
    input_fact_ids: tuple[str, ...]
    run_id: str
    created_at: str

    @property
    def code(self) -> str:
        """computed | not_applicable | blocked_by_validation | missing_input | ..."""
        return str(self.details.get("code", ""))

    @property
    def computed(self) -> bool:
        return self.status is MetricStatus.COMPUTED

    @property
    def basis_change(self) -> list[dict[str, Any]]:
        """Recorded accounting-policy or restatement events the input periods straddle."""
        return list(self.details.get("basis_change", []))

    @property
    def basis_used(self) -> str:
        """The basis of the facts used (differs from `basis` after a standalone fallback)."""
        return str(self.details.get("basis_used", self.basis))

    @property
    def warnings(self) -> list[dict[str, Any]]:
        """Validation warnings on the input concepts and periods."""
        return list(self.details.get("warnings", []))


@dataclass
class MetricRunReport:
    """What one run did."""

    run_id: str
    isin: str
    written: int = 0
    unchanged: int = 0
    by_code: Counter[str] = field(default_factory=Counter)

    @property
    def computed(self) -> int:
        return self.by_code[COMPUTED]

    @property
    def not_computed(self) -> dict[str, int]:
        """Reason code -> number of metric values that could not be computed."""
        return {k: v for k, v in sorted(self.by_code.items()) if k != COMPUTED}
