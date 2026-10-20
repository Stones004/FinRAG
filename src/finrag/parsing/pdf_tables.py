"""Statement tables in PDFs -> ParsedFacts (label, period, value, location, confidence)."""

from __future__ import annotations

import io
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from finrag.core.config import ConfigError, PdfSettings, Settings, get_settings
from finrag.core.fiscal import period_to_label
from finrag.core.types import Basis, Period, PeriodType
from finrag.parsing.models import EXTRACTOR_OCR, EXTRACTOR_TABLE, ParsedFact
from finrag.parsing.numbers import NotANumber, parse_number
from finrag.parsing.page_classifier import PageLabel
from finrag.parsing.pdf_text import METHOD_OCR, PageText
from finrag.parsing.sections import CompiledColumnCheck, Row, SectionPatterns
from finrag.parsing.table_layout import (
    Line,
    Table,
    TableParams,
    Word,
    build_table,
    date_spans,
    words_in_span,
)

BALANCE_SHEET = "balance_sheet"


@dataclass(frozen=True)
class ColumnPeriod:
    """What a column header says: the period end, and the period type if it could be read."""

    end: date | None
    kind: PeriodType | None
    text: str  # the header text, for the review payload


@dataclass
class TablesResult:
    """Facts from all statement pages plus what could not be read and items for review."""

    facts: list[ParsedFact] = field(default_factory=list)
    review_items: list[dict[str, object]] = field(default_factory=list)
    pages_read: list[int] = field(default_factory=list)
    pages_unread: list[dict[str, object]] = field(default_factory=list)


def find_date(text: str, patterns: SectionPatterns) -> date | None:
    """Header text -> the first real date any date pattern finds (earliest position wins)."""
    found = date_spans(text, patterns)
    return min(found, key=lambda item: item[0])[2] if found else None


def period_start(end: date, kind: PeriodType, patterns: SectionPatterns) -> date:
    """Period end + type -> first day of the period (whole months back from the end's month)."""
    months = patterns.months_in_kind[kind]
    index = end.year * 12 + (end.month - 1) - (months - 1)
    return date(index // 12, index % 12 + 1, 1)


def kind_fits(end: date, kind: PeriodType, patterns: SectionPatterns) -> bool:
    """True if a period of this type ending on `end` is a real fiscal period.

    A column's length must match its header type: "Year ended 30.06.2025" would be twelve months
    from July, which is no fiscal year of a March year-end, so that column is a quarter.
    """
    if kind is PeriodType.INSTANT:
        return True
    try:
        period_to_label(Period(period_start(end, kind, patterns), end, kind))
    except (ValueError, KeyError):
        return False
    return True


def _kind_labels(
    lines: Sequence[Line], patterns: SectionPatterns
) -> list[tuple[float, PeriodType]]:
    """Header lines -> (x centre, type) for each period-kind phrase ('Quarter ended') found."""
    labels: list[tuple[float, PeriodType]] = []
    for line in lines:
        text = line.text
        taken: list[tuple[int, int]] = []
        for kind, pats in patterns.kinds:
            for pattern in pats:
                for match in pattern.finditer(text):
                    span = (match.start(), match.end())
                    if any(span[0] < b and a < span[1] for a, b in taken):
                        continue
                    taken.append(span)
                    covered = words_in_span(line, span)
                    if covered:
                        centre = (min(w.x0 for w in covered) + max(w.x1 for w in covered)) / 2
                        labels.append((centre, kind))
    return labels


def _runs(line: Line, gap: float) -> list[tuple[Word, ...]]:
    """Split a line into runs of words separated by gaps wider than `gap` (separate cells)."""
    runs: list[list[Word]] = []
    for word in line.words:
        if runs and word.x0 - runs[-1][-1].x1 <= gap:
            runs[-1].append(word)
        else:
            runs.append([word])
    return [tuple(run) for run in runs]


def read_headers(
    table: Table, patterns: SectionPatterns, statement_type: str | None, gap: float
) -> list[ColumnPeriod]:
    """Table header lines -> one ColumnPeriod per column (end date, period kind, header text).

    Only header text that starts inside the numeric columns counts: a title line that runs on
    from the label area into the columns ("... QUARTER AND YEAR ENDED MARCH 31, 2026") is
    ignored. `gap` is the space between words that separates two header cells.
    """
    count = len(table.columns)
    lower_x, upper_x = table.bounds(0)[0], table.columns[-1].right
    slack = (upper_x - lower_x) / (2 * count)  # half a column: header text may overhang the cell
    in_region = [
        Line(
            tuple(
                w
                for run in _runs(line, gap)
                if lower_x <= (run[0].x0 + run[0].x1) / 2
                for w in run
                if (w.x0 + w.x1) / 2 <= upper_x + slack
            )
        )
        for line in table.header_lines
    ]
    in_region = [line for line in in_region if line.words]
    per_column: list[list[Line]] = []
    centres: list[float] = []
    for index in range(count):
        low, high = table.bounds(index)
        centres.append((low + high) / 2)
        cell_lines = []
        for line in in_region:
            mine = tuple(
                w
                for w in line.words
                if low < (w.x0 + w.x1) / 2 <= high + (slack if index == count - 1 else 0)
            )
            if mine:
                cell_lines.append(Line(mine))
        per_column.append(cell_lines)
    kinds = _kind_labels(in_region, patterns)
    periods: list[ColumnPeriod] = []
    for index in range(count):
        nearest_first = list(reversed(per_column[index]))  # closest to the data first
        end = next(
            (d for d in (find_date(line.text, patterns) for line in nearest_first) if d), None
        ) or find_date(" ".join(line.text for line in per_column[index]), patterns)
        kind = None
        if kinds:
            # a "Quarter ended" label sits over one of its columns, not over each: the nearest label
            # that makes a real fiscal period of this column's date wins
            ranked = sorted(kinds, key=lambda item: abs(item[0] - centres[index]))
            kind = ranked[0][1]
            if end is not None:
                kind = next((k for _, k in ranked if kind_fits(end, k, patterns)), kind)
        if statement_type == BALANCE_SHEET:
            kind = PeriodType.INSTANT
        periods.append(ColumnPeriod(end, kind, " ".join(line.text for line in per_column[index])))
    return _distinct(periods, kinds, centres, patterns)


def _distinct(
    periods: list[ColumnPeriod],
    kinds: list[tuple[float, PeriodType]],
    centres: list[float],
    patterns: SectionPatterns,
) -> list[ColumnPeriod]:
    """No two columns of a table are the same period.

    A date such as 31.03.2025 fits a quarter and a year; when two columns end up with the same
    end date and type, the one whose header label is farther away takes the next fitting type
    (the third column of "Quarter ended: 31.03.26, 31.12.25, 31.03.25 | Year ended: 31.03.26,
    31.03.25" is the quarter, not a second 2025 year).
    """
    out = list(periods)
    seen: dict[tuple[date, PeriodType], int] = {}

    def distance(index: int, kind: PeriodType) -> float:
        return min((abs(x - centres[index]) for x, k in kinds if k is kind), default=0.0)

    for index, period in enumerate(out):
        if period.end is None or period.kind is None:
            continue
        key = (period.end, period.kind)
        other = seen.get(key)
        if other is None:
            seen[key] = index
            continue
        loser = max((index, other), key=lambda i: distance(i, period.kind))
        alternatives = sorted(
            {k for _, k in kinds if k is not period.kind and kind_fits(period.end, k, patterns)},
            key=lambda k: distance(loser, k),
        )
        if alternatives:
            out[loser] = ColumnPeriod(out[loser].end, alternatives[0], out[loser].text)
            seen[key] = index if loser == other else other
            seen.setdefault((period.end, alternatives[0]), loser)
    return out


def _confidence(table: Table, periods: Sequence[ColumnPeriod]) -> dict[str, float]:
    """Header found, column-count consistency, numeric ratio (each 0..1) and their mean."""
    dated = [p for p in periods if p.end]  # a note column has no date and gives no facts
    header = sum(1 for p in dated if p.kind) / len(dated) if dated else 0.0
    modal = statistics.mode(table.row_cell_counts)
    consistency = sum(1 for c in table.row_cell_counts if c == modal) / len(table.row_cell_counts)
    numeric = table.numeric_words / table.zone_words if table.zone_words else 0.0
    return {
        "header_found": header,
        "column_consistency": consistency,
        "numeric_ratio": numeric,
        "overall": (header + consistency + numeric) / 3,
    }


def _decimal_places(text: str) -> int | None:
    """Printed number -> its number of decimal places (None for a dash or a non-number)."""
    try:
        value = parse_number(text)
    except NotANumber:
        return None
    return None if value is None else -int(value.as_tuple().exponent)


def format_outliers(table: Table, periods: Sequence[ColumnPeriod]) -> set[tuple[int, int]]:
    """(row index, column) of numbers whose decimal places differ from the table's usual count.

    A statement prints all amounts with the same decimals; OCR often drops a decimal point
    ("948.22" read as "94822"), which no other check can see.
    """
    cells = [
        (row.index, column, _decimal_places(text))
        for row in table.rows
        for column, text in row.cells.items()
        if periods[column].end
    ]
    counted = [places for _, _, places in cells if places is not None]
    if not counted:
        return set()
    usual = statistics.mode(counted)
    return {(r, c) for r, c, places in cells if places is not None and places != usual}


def _clean_label(label: str) -> str:
    """Row label -> lower case, leading numbering and punctuation removed, spaces collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"^[^A-Za-z]+", "", label)).strip().lower()


def _find_row(table: Table, alternatives: Row) -> int | None:
    """Index of the first row whose label matches the first matching pattern (priority order)."""
    for pattern in alternatives:
        for row in table.rows:
            if pattern.search(_clean_label(row.label)):
                return row.index
    return None


def _cell(table: Table, row_index: int, column: int) -> Decimal | None:
    """The printed number at (row, column), or None (no cell, a dash, not a number)."""
    for row in table.rows:
        if row.index == row_index and column in row.cells:
            try:
                return parse_number(row.cells[column])
            except NotANumber:
                return None
    return None


def _holds(total: Decimal, computed: Decimal, abs_tol: float, rel_tol: float) -> bool:
    """True when two figures agree within the rounding tolerance (either bound is enough)."""
    diff = abs(total - computed)
    return diff <= Decimal(str(abs_tol)) or diff <= Decimal(str(rel_tol)) * abs(total)


@dataclass(frozen=True)
class _Resolved:
    """A column check with its rows found in one table."""

    total: int
    plus: list[int]  # required and optional parts present
    minus: list[int]
    required: list[int]


def _resolve(table: Table, check: CompiledColumnCheck) -> _Resolved | None:
    """Find the rows of a check in a table; None if a required row is missing."""
    total = _find_row(table, check.total)
    parts = [_find_row(table, alternatives) for alternatives in check.parts]
    minus = [_find_row(table, alternatives) for alternatives in check.minus]
    if total is None or None in parts or None in minus:
        return None
    optional = [_find_row(table, alternatives) for alternatives in check.optional_parts]
    plus = [r for r in parts if r is not None] + [r for r in optional if r is not None]
    cleaned_minus = [r for r in minus if r is not None]
    return _Resolved(total, plus, cleaned_minus, [total, *plus[: len(parts)], *cleaned_minus])


def _column_holds(rows: _Resolved, values: dict[int, Decimal], pdf: PdfSettings) -> bool:
    """True when total = sum(plus rows present) - sum(minus rows) within the rounding tolerance."""
    computed = sum((values[r] for r in rows.plus if r in values), Decimal(0))
    computed -= sum((values[r] for r in rows.minus), Decimal(0))
    return _holds(
        values[rows.total], computed, pdf.sum_check_abs_tolerance, pdf.sum_check_rel_tolerance
    )


def sum_shifted(
    table: Table,
    periods: Sequence[ColumnPeriod],
    statement_type: str | None,
    checks: Sequence[CompiledColumnCheck],
    settings: Settings,
) -> set[tuple[int, int]]:
    """(row index, column) of cells that, made 100x larger or smaller, fix a failing column check.

    Each registry check (`column_checks` of the sections registry) is evaluated in each dated column
    where all its required rows are present. If it fails, each of its cells is tried at
    value x factor and value / factor; a cell for which the check then holds is returned.
    """
    pdf = settings.parsing.pdf
    factor = Decimal(pdf.sum_shift_factor)
    found: set[tuple[int, int]] = set()
    for check in checks:
        rows = _resolve(table, check) if check.statement == statement_type else None
        if rows is None:
            continue
        for column, period in enumerate(periods):
            if period.end is None:
                continue
            cells = {r: _cell(table, r, column) for r in {rows.total, *rows.plus, *rows.minus}}
            if any(cells[r] is None for r in rows.required):
                continue
            values = {r: v for r, v in cells.items() if v is not None}
            if _column_holds(rows, values, pdf):
                continue
            for row_index, value in values.items():
                for shifted in (value * factor, value / factor):
                    if _column_holds(rows, {**values, row_index: shifted}, pdf):
                        found.add((row_index, column))
    return found


_GHOST_OVERLAP = 0.5  # share of a space's width that another glyph must cover


def ghost_spaces(chars: Sequence[dict]) -> set[tuple[float, float, str]]:
    """Space glyphs that sit on top of another glyph of the same line (keys of the chars).

    Some producers pad a right-aligned number with space glyphs that overlap its first digits;
    sorted by position such a space lands between two digits and cuts "10,716.81" into "1" and
    "0,716.81". A real word gap never overlaps a glyph.
    """
    lines: dict[int, list[dict]] = {}
    for char in chars:
        lines.setdefault(round(char["top"]), []).append(char)
    ghosts: set[tuple[float, float, str]] = set()
    for line in lines.values():
        glyphs = [c for c in line if not c["text"].isspace()]
        for space in (c for c in line if c["text"].isspace()):
            width = space["x1"] - space["x0"]
            if width > 0 and any(
                min(g["x1"], space["x1"]) - max(g["x0"], space["x0"]) > _GHOST_OVERLAP * width
                for g in glyphs
            ):
                ghosts.add(_char_key(space))
    return ghosts


def _char_key(char: dict) -> tuple[float, float, str]:
    return (round(char["x0"], 2), round(char["top"], 2), char["text"])


def _native_words(pdf_page: object) -> list[Word]:
    """A pdfplumber page -> positioned words (ghost spaces left out)."""
    ghosts = ghost_spaces(pdf_page.chars)  # type: ignore[attr-defined]
    if ghosts:
        pdf_page = pdf_page.filter(  # type: ignore[attr-defined]
            lambda o: o.get("object_type") != "char" or _char_key(o) not in ghosts
        )
    return [
        Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"])
        for w in pdf_page.extract_words()  # type: ignore[attr-defined]
    ]


def _unit_scale(unit_hint: str | None, settings: Settings) -> int | None:
    """Unit key -> multiplier into rupees; None if the page named no unit."""
    if unit_hint is None:
        return None
    try:
        return settings.units.multipliers[unit_hint]
    except KeyError as exc:
        raise ConfigError(f"unit '{unit_hint}' is not in settings.units.multipliers") from exc


def _facts_for_table(
    table: Table,
    periods: Sequence[ColumnPeriod],
    page: PageText,
    label: PageLabel,
    filing_basis: Basis,
    patterns: SectionPatterns,
    settings: Settings,
    confidence: float,
    outliers: set[tuple[int, int]],
    shifted: set[tuple[int, int]],
) -> list[ParsedFact]:
    """One ParsedFact per numeric cell in a column that has a period end date.

    A number in `outliers` gets the lower format_outlier_confidence; a cell in `shifted` (a sum
    check holds if it were 100x larger or smaller) gets sum_shift_confidence.
    """
    pdf = settings.parsing.pdf
    if label.unit_from_page:  # the unit was taken from another page: slightly less sure
        confidence = max(0.0, confidence - pdf.unit_inherited_confidence_penalty)
    suffix = f";unit_page={label.unit_from_page}" if label.unit_from_page else ""
    outlier_confidence = min(confidence, pdf.format_outlier_confidence)
    shifted_confidence = min(confidence, pdf.sum_shift_confidence)
    is_ocr = page.method == METHOD_OCR
    scale = _unit_scale(label.unit_hint, settings)
    basis = label.basis if label.basis is not Basis.UNKNOWN else filing_basis
    facts: list[ParsedFact] = []
    for row in table.rows:
        for column, text in sorted(row.cells.items()):
            period = periods[column]
            if period.end is None:
                continue
            try:
                value = parse_number(text)
            except NotANumber:
                continue  # cannot happen for placed cells; kept so a bad token never aborts a page
            if period.kind is PeriodType.INSTANT:
                start: date | None = period.end
            elif period.kind is not None:
                start = period_start(period.end, period.kind, patterns)
            else:
                start = None
            facts.append(
                ParsedFact(
                    value=value,
                    concept_raw=None,
                    label_raw=row.label,
                    period_start=start,
                    period_end=period.end,
                    basis=basis.value,
                    unit_raw=label.unit_hint,
                    scale=scale,
                    decimals=None,
                    source_loc=f"{page.page}:{row.index}:{column + 1}{suffix}",
                    statement_type=label.statement_type,
                    extractor=EXTRACTOR_OCR if is_ocr else EXTRACTOR_TABLE,
                    ocr_backend=page.ocr_backend if is_ocr else None,
                    confidence=min(
                        outlier_confidence if (row.index, column) in outliers else confidence,
                        shifted_confidence if (row.index, column) in shifted else confidence,
                    ),
                )
            )
    return facts


def extract_statement_tables(
    data: bytes,
    pages: Sequence[PageText],
    labels: Sequence[PageLabel],
    patterns: SectionPatterns,
    filing_basis: Basis,
    settings: Settings | None = None,
) -> TablesResult:
    """Statement pages -> facts. Tables below min_table_confidence also give a review item.

    A labelled page with no readable table, or no period header, is listed in pages_unread and
    queued for review: nothing is dropped silently.
    """
    import pdfplumber

    settings = settings or get_settings()
    pdf = settings.parsing.pdf
    params = TableParams(
        pdf.row_tolerance_pt,
        pdf.column_tolerance_pt,
        pdf.min_table_rows,
        pdf.header_max_lines,
        pdf.column_min_support_ratio,
    )
    by_page = {p.page: p for p in pages}
    result = TablesResult()
    wanted = [lb for lb in labels if lb.statement_type]
    if not wanted:
        return result
    with pdfplumber.open(io.BytesIO(data)) as doc:
        for label in wanted:
            page = by_page[label.page]
            if page.method == METHOD_OCR:
                words: list[Word] = list(page.words)
                missing = "OCR gave no words"
            elif doc.pages[label.page - 1].rotation:
                words = []
                missing = "rotated page: native word positions are not trusted; OCR needed"
            else:
                words = _native_words(doc.pages[label.page - 1])
                missing = "no positioned words on the page"
            if not words:
                _unread(result, label, missing)
                continue
            table = build_table(words, patterns, params)
            if table is None:
                _unread(result, label, "no numeric columns found")
                continue
            periods = read_headers(table, patterns, label.statement_type, pdf.column_tolerance_pt)
            scores = _confidence(table, periods)
            outliers = format_outliers(table, periods)
            shifted = sum_shifted(
                table, periods, label.statement_type, patterns.column_checks, settings
            )
            facts = _facts_for_table(
                table,
                periods,
                page,
                label,
                filing_basis,
                patterns,
                settings,
                scores["overall"],
                outliers,
                shifted,
            )
            if not facts:
                _unread(result, label, "no column header with a period date")
                continue
            result.pages_read.append(label.page)
            result.facts.extend(facts)
            if scores["overall"] < pdf.min_table_confidence or outliers or shifted:
                labels_by_row = {r.index: r.label for r in table.rows}
                result.review_items.append(
                    {
                        "page": label.page,
                        "statement_type": label.statement_type,
                        "confidence": round(scores["overall"], 3),
                        "components": {k: round(v, 3) for k, v in scores.items()},
                        "headers": [p.text for p in periods],
                        "format_outliers": [
                            {
                                "source_loc": f"{label.page}:{row}:{column + 1}",
                                "label": labels_by_row[row],
                            }
                            for row, column in sorted(outliers)
                        ],
                        "sum_shifted": [
                            {
                                "source_loc": f"{label.page}:{row}:{column + 1}",
                                "label": labels_by_row[row],
                            }
                            for row, column in sorted(shifted)
                        ],
                    }
                )
    return result


def _unread(result: TablesResult, label: PageLabel, reason: str) -> None:
    """Record a labelled page that gave no facts, and queue it for review."""
    entry = {"page": label.page, "statement_type": label.statement_type, "reason": reason}
    result.pages_unread.append(entry)
    result.review_items.append({**entry, "confidence": 0.0})
