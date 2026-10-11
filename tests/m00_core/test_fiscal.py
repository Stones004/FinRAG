from datetime import date

import pytest

from finrag.core.config import FiscalSettings
from finrag.core.fiscal import (
    components_of,
    cumulative_steps,
    fiscal_year_of,
    is_cumulative,
    label_to_period,
    period_to_label,
    shift_period,
)
from finrag.core.types import Period, PeriodType

APRIL = FiscalSettings(fy_start_month=4, fy_label_format="FY{yy}")
JANUARY = FiscalSettings(fy_start_month=1, fy_label_format="FY{yy}")
OCTOBER = FiscalSettings(fy_start_month=10, fy_label_format="FY{yy}")
ALL_LABELS = ["Q1 FY26", "Q2 FY25", "Q3 FY24", "Q4 FY26", "H1 FY26", "H2 FY26", "9M FY26", "FY26"]


def labels(periods: list[Period], fiscal: FiscalSettings = APRIL) -> list[str]:
    return [period_to_label(p, fiscal) for p in periods]


def test_label_to_period_april() -> None:
    q1 = label_to_period("Q1 FY26", APRIL)
    assert (q1.start, q1.end, q1.type) == (date(2025, 4, 1), date(2025, 6, 30), PeriodType.Q)
    fy = label_to_period("FY26", APRIL)
    assert (fy.start, fy.end, fy.type) == (date(2025, 4, 1), date(2026, 3, 31), PeriodType.FY)
    assert label_to_period("Q4 FY26", APRIL).end == date(2026, 3, 31)
    assert label_to_period("H2 FY26", APRIL).start == date(2025, 10, 1)
    assert label_to_period("9M FY26", APRIL).end == date(2025, 12, 31)


@pytest.mark.parametrize("fiscal", [APRIL, JANUARY, OCTOBER])
def test_labels_round_trip(fiscal: FiscalSettings) -> None:
    for label in ALL_LABELS:
        assert period_to_label(label_to_period(label, fiscal), fiscal) == label


def test_label_parsing_is_tolerant_but_strict_on_garbage() -> None:
    assert label_to_period("  q1 fy26 ", APRIL) == label_to_period("Q1 FY26", APRIL)
    for bad in ["Q5 FY26", "FY2026", "Q1", "H3 FY26", ""]:
        with pytest.raises(ValueError):
            label_to_period(bad, APRIL)


def test_custom_label_format() -> None:
    fiscal = FiscalSettings(fy_start_month=4, fy_label_format="FY-{yy}")
    assert period_to_label(label_to_period("Q2 FY-26", fiscal), fiscal) == "Q2 FY-26"


def test_fiscal_year_of() -> None:
    assert fiscal_year_of(date(2025, 4, 1), APRIL) == 2026
    assert fiscal_year_of(date(2026, 3, 31), APRIL) == 2026
    assert fiscal_year_of(date(2025, 12, 31), JANUARY) == 2025
    assert fiscal_year_of(date(2025, 10, 1), OCTOBER) == 2026


def test_january_fiscal_year() -> None:
    q1 = label_to_period("Q1 FY26", JANUARY)
    assert (q1.start, q1.end) == (date(2026, 1, 1), date(2026, 3, 31))
    fy = label_to_period("FY26", JANUARY)
    assert (fy.start, fy.end) == (date(2026, 1, 1), date(2026, 12, 31))


def test_shift_same_period_last_year() -> None:
    q3 = label_to_period("Q3 FY26", APRIL)
    assert period_to_label(shift_period(q3, years=-1, fiscal=APRIL), APRIL) == "Q3 FY25"
    q3j = label_to_period("Q3 FY26", JANUARY)
    assert period_to_label(shift_period(q3j, years=-1, fiscal=JANUARY), JANUARY) == "Q3 FY25"


def test_shift_by_periods_crosses_year_boundaries() -> None:
    q1 = label_to_period("Q1 FY26", APRIL)
    assert period_to_label(shift_period(q1, periods=-1, fiscal=APRIL), APRIL) == "Q4 FY25"
    assert period_to_label(shift_period(q1, periods=5, fiscal=APRIL), APRIL) == "Q2 FY27"
    h2 = label_to_period("H2 FY26", APRIL)
    assert period_to_label(shift_period(h2, periods=1, fiscal=APRIL), APRIL) == "H1 FY27"
    fy = label_to_period("FY26", APRIL)
    assert period_to_label(shift_period(fy, periods=-1, fiscal=APRIL), APRIL) == "FY25"


def test_instant_and_misaligned_periods_are_rejected() -> None:
    instant = Period(date(2026, 3, 31), date(2026, 3, 31), PeriodType.INSTANT)
    with pytest.raises(ValueError):
        period_to_label(instant, APRIL)
    with pytest.raises(ValueError):
        shift_period(instant, years=-1, fiscal=APRIL)
    misaligned = Period(date(2025, 5, 1), date(2025, 7, 31), PeriodType.Q)
    with pytest.raises(ValueError):
        period_to_label(misaligned, APRIL)
    wrong_type = Period(date(2025, 4, 1), date(2025, 6, 30), PeriodType.H)
    with pytest.raises(ValueError):
        period_to_label(wrong_type, APRIL)


def test_is_cumulative() -> None:
    flags = {
        s: is_cumulative(label_to_period(s, APRIL))
        for s in ["Q1 FY26", "H1 FY26", "9M FY26", "FY26"]
    }
    assert flags == {"Q1 FY26": False, "H1 FY26": True, "9M FY26": True, "FY26": True}


def test_components_of() -> None:
    def comp(label: str, fiscal: FiscalSettings = APRIL) -> list[str]:
        return labels(components_of(label_to_period(label, fiscal), fiscal), fiscal)

    assert comp("FY26") == ["Q1 FY26", "Q2 FY26", "Q3 FY26", "Q4 FY26"]
    assert comp("H1 FY26") == ["Q1 FY26", "Q2 FY26"]
    assert comp("H2 FY26") == ["Q3 FY26", "Q4 FY26"]
    assert comp("9M FY26") == ["Q1 FY26", "Q2 FY26", "Q3 FY26"]
    assert comp("Q1 FY26") == []
    assert comp("FY26", JANUARY) == ["Q1 FY26", "Q2 FY26", "Q3 FY26", "Q4 FY26"]


def _identity(label: str) -> list[str]:
    steps = cumulative_steps(label_to_period(label, APRIL), APRIL)
    return [
        " ".join(f"{'+' if t.sign > 0 else '-'}{period_to_label(t.period, APRIL)}" for t in s.terms)
        for s in steps
    ]


def test_cumulative_steps_required_identities() -> None:
    assert _identity("H1 FY26") == ["+Q1 FY26 +Q2 FY26"]
    assert _identity("9M FY26") == ["+H1 FY26 +Q3 FY26"]
    assert _identity("FY26")[0] == "+9M FY26 +Q4 FY26"
    assert _identity("H2 FY26") == ["+FY26 -H1 FY26"]


def test_cumulative_steps_discrete_quarters() -> None:
    assert _identity("Q2 FY26") == ["+H1 FY26 -Q1 FY26"]
    assert _identity("Q3 FY26") == ["+9M FY26 -H1 FY26"]
    assert _identity("Q4 FY26")[0] == "+FY26 -9M FY26"
    assert _identity("Q1 FY26") == []


def test_cumulative_steps_alternative_routes_ordered_by_derived_inputs() -> None:
    assert _identity("FY26") == ["+9M FY26 +Q4 FY26", "+H1 FY26 +H2 FY26"]
    assert _identity("Q4 FY26") == ["+FY26 -9M FY26", "+H2 FY26 -Q3 FY26"]


@pytest.mark.parametrize("label", ["Q1FY27", "Q1 FY27", "q1fy27", "Q1  FY27"])
def test_label_to_period_space_is_optional(label: str) -> None:
    assert label_to_period(label, APRIL) == label_to_period("Q1 FY27", APRIL)
    assert period_to_label(label_to_period(label, APRIL), APRIL) == "Q1 FY27"


def test_label_to_period_no_space_forms() -> None:
    assert label_to_period("H2FY26", APRIL) == label_to_period("H2 FY26", APRIL)
    assert label_to_period("9MFY26", APRIL) == label_to_period("9M FY26", APRIL)
    assert period_to_label(label_to_period("H2FY26", APRIL), APRIL) == "H2 FY26"
    assert period_to_label(label_to_period("9MFY26", APRIL), APRIL) == "9M FY26"
    assert label_to_period("FY26", APRIL).type is PeriodType.FY
