"""The M03 part of the sections registry, validated and compiled."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from finrag.core.config import ConfigError, Settings, get_settings, load_registry
from finrag.core.types import PeriodType


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class PeriodKind(_Section):
    """A column-header word group that names a period type."""

    type: PeriodType
    patterns: list[str]


class TableHeader(_Section):
    """How to read the column headers of a statement table."""

    date_patterns: list[str]
    period_kinds: list[PeriodKind]
    months_in_kind: dict[PeriodType, int]


class TableLabels(_Section):
    """How wrapped row labels are joined."""

    continuation_words: list[str]


class ColumnCheck(_Section):
    """One sum check inside a statement column: total = parts + optional parts - minus."""

    id: str
    statement: str
    total: list[str]
    parts: list[list[str]]
    minus: list[list[str]] = []
    optional_parts: list[list[str]] = []


class SectionsRegistry(_Section):
    """statement_pages, basis_patterns, unit_patterns, table_header, financial_terms, checks."""

    statement_pages: dict[str, list[str]]
    basis_patterns: dict[str, list[str]]
    unit_patterns: dict[str, list[str]]
    table_header: TableHeader
    table_labels: TableLabels
    financial_terms: list[str]
    column_checks: list[ColumnCheck] = []
    basis_title_patterns: list[str] = []

    @field_validator("statement_pages", "basis_patterns", "unit_patterns")
    @classmethod
    def _compile(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        """Every pattern must be a valid regular expression."""
        for patterns in value.values():
            for pattern in patterns:
                re.compile(pattern)
        return value

    @field_validator("basis_title_patterns")
    @classmethod
    def _compile_titles(cls, value: list[str]) -> list[str]:
        for pattern in value:
            re.compile(pattern)
        return value


Row = list[re.Pattern[str]]  # label regexes of one row, in priority order


@dataclass(frozen=True)
class CompiledColumnCheck:
    """A column check with its row labels compiled."""

    id: str
    statement: str
    total: Row
    parts: list[Row]
    minus: list[Row]
    optional_parts: list[Row]


@dataclass(frozen=True)
class SectionPatterns:
    """Compiled regexes ready for the classifier and table reader."""

    statements: dict[str, list[re.Pattern[str]]]
    basis: dict[str, list[re.Pattern[str]]]
    units: dict[str, list[re.Pattern[str]]]
    dates: list[re.Pattern[str]]
    kinds: list[tuple[PeriodType, list[re.Pattern[str]]]]
    months_in_kind: dict[PeriodType, int]
    continuation_words: frozenset[str]
    financial_terms: frozenset[str]
    column_checks: list[CompiledColumnCheck]
    basis_titles: list[re.Pattern[str]] = field(default_factory=list)


def _compile_all(groups: dict[str, list[str]]) -> dict[str, list[re.Pattern[str]]]:
    """key -> patterns, compiled case-insensitively."""
    return {k: [re.compile(p, re.IGNORECASE) for p in v] for k, v in groups.items()}


def _row(patterns: list[str]) -> Row:
    """Label regexes of one row -> compiled, case-insensitive."""
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def _compile_check(check: ColumnCheck) -> CompiledColumnCheck:
    """A registry column check -> compiled row patterns."""
    return CompiledColumnCheck(
        id=check.id,
        statement=check.statement,
        total=_row(check.total),
        parts=[_row(p) for p in check.parts],
        minus=[_row(p) for p in check.minus],
        optional_parts=[_row(p) for p in check.optional_parts],
    )


def load_section_patterns(settings: Settings | None = None) -> SectionPatterns:
    """Read and validate the M03 keys of the sections registry -> compiled patterns."""
    settings = settings or get_settings()
    raw = load_registry("sections")
    try:
        registry = SectionsRegistry.model_validate(raw)
    except (ValidationError, re.error) as exc:
        raise ConfigError(f"sections registry is invalid: {exc}") from exc
    unknown = set(registry.unit_patterns) - set(settings.units.multipliers)
    if unknown:
        raise ConfigError(
            f"sections registry: unit_patterns keys {sorted(unknown)} are not in "
            "settings.units.multipliers"
        )
    header = registry.table_header
    return SectionPatterns(
        statements=_compile_all(registry.statement_pages),
        basis=_compile_all(registry.basis_patterns),
        units=_compile_all(registry.unit_patterns),
        dates=[re.compile(p, re.IGNORECASE) for p in header.date_patterns],
        kinds=[
            (k.type, [re.compile(p, re.IGNORECASE) for p in k.patterns])
            for k in header.period_kinds
        ],
        months_in_kind=dict(header.months_in_kind),
        continuation_words=frozenset(w.lower() for w in registry.table_labels.continuation_words),
        financial_terms=frozenset(t.lower() for t in registry.financial_terms),
        column_checks=[_compile_check(c) for c in registry.column_checks],
        basis_titles=[re.compile(p, re.IGNORECASE) for p in registry.basis_title_patterns],
    )
