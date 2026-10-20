"""Words with positions -> lines -> numeric columns -> rows of (label, cells).

Works on any positioned words (a PDF text layer through pdfplumber, or Tesseract output). A
column is a group of numbers whose right edges line up on at least `min_table_rows` lines; a
number that is not in such a column (a note reference inside a label, a serial number) stays part
of the label.
"""

from __future__ import annotations

import calendar
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from finrag.parsing.numbers import is_number_token
from finrag.parsing.ocr.base import OcrWord as Word
from finrag.parsing.sections import SectionPatterns

_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}
# A note reference such as "35a": sits in the note column, is not a number, and is not label text.
_NOTE_REF = re.compile(r"^\d{1,3}[A-Za-z]$")

__all__ = [
    "Column",
    "Line",
    "Row",
    "Table",
    "TableParams",
    "Word",
    "build_table",
    "date_spans",
    "group_lines",
    "words_in_span",
]


@dataclass(frozen=True)
class Line:
    """Words on one visual line, left to right."""

    words: tuple[Word, ...]

    @property
    def center(self) -> float:
        """Mean vertical centre of the words."""
        return statistics.fmean((w.top + w.bottom) / 2 for w in self.words)

    @property
    def text(self) -> str:
        """The words joined by single spaces."""
        return " ".join(w.text for w in self.words)


@dataclass(frozen=True)
class Column:
    """A numeric column: where its numbers' right edges are, and how many lines support it."""

    right: float
    support: int


@dataclass(frozen=True)
class Row:
    """One table row: its label and its numeric cells by 0-based column index."""

    index: int  # 1-based position among the page's lines (used in source_loc)
    label: str
    cells: dict[int, str]
    y: float = 0.0  # vertical centre of the line holding the numbers


@dataclass
class Table:
    """The table found on one page."""

    columns: list[Column]
    rows: list[Row]
    header_lines: list[Line]
    numeric_words: int = 0  # numeric tokens placed in a column
    zone_words: int = 0  # all tokens of numeric rows that sit in the numeric zone
    row_cell_counts: list[int] = field(default_factory=list)

    @property
    def zone_left(self) -> float:
        """Left edge of the first column's cells (estimated from the column spacing)."""
        return self.bounds(0)[0]

    def bounds(self, column: int) -> tuple[float, float]:
        """(lower, upper) x-limits of a column's cell, used to place header words."""
        rights = [c.right for c in self.columns]
        if column > 0:
            lower = rights[column - 1]
        elif len(rights) > 1:
            lower = rights[0] - statistics.median(
                b - a for a, b in zip(rights, rights[1:], strict=False)
            )
        else:
            lower = rights[0] - 1
        return lower, rights[column]


def _center(word: Word) -> float:
    """Horizontal centre of a word."""
    return (word.x0 + word.x1) / 2


def group_lines(words: Sequence[Word], tolerance: float) -> list[Line]:
    """Words -> lines: words whose vertical centres are within `tolerance` of the line's mean."""
    ordered = sorted(words, key=lambda w: ((w.top + w.bottom) / 2, w.x0))
    groups: list[list[Word]] = []
    for word in ordered:
        centre = (word.top + word.bottom) / 2
        if (
            groups
            and abs(centre - statistics.fmean((w.top + w.bottom) / 2 for w in groups[-1]))
            <= tolerance
        ):
            groups[-1].append(word)
        else:
            groups.append([word])
    return [Line(tuple(sorted(g, key=lambda w: w.x0))) for g in groups]


def words_in_span(line: Line, span: tuple[int, int]) -> list[Word]:
    """The words of a line whose characters overlap the [start, end) span of line.text."""
    covered: list[Word] = []
    position = 0
    for word in line.words:
        start, end = position, position + len(word.text)
        if start < span[1] and span[0] < end:
            covered.append(word)
        position = end + 1  # the joining space
    return covered


def _date_from(match: re.Match[str]) -> date | None:
    """A date-pattern match -> date, or None if the parts do not make a real date."""
    groups = match.groupdict()
    try:
        month = int(groups["m"]) if groups.get("m") else _MONTHS[groups["mon"][:3].lower()]
        return date(int(groups["y"]), month, int(groups["d"]))
    except (KeyError, ValueError, TypeError):
        return None


def date_spans(text: str, patterns: SectionPatterns) -> list[tuple[int, int, date]]:
    """Text -> (start, end, date) for each real date found ('tax 94822' is not a date)."""
    spans = []
    for pattern in patterns.dates:
        for match in pattern.finditer(text):
            parsed = _date_from(match)
            if parsed:
                spans.append((match.start(), match.end(), parsed))
    return spans


def numeric_words(line: Line, patterns: SectionPatterns) -> list[Word]:
    """The words of a line that are printed numbers, leaving out the parts of dates ('2026')."""
    dated = {
        id(w)
        for start, end, _ in date_spans(line.text, patterns)
        for w in words_in_span(line, (start, end))
    }
    return [w for w in line.words if is_number_token(w.text) and id(w) not in dated]


def find_columns(
    lines: Sequence[Line],
    patterns: SectionPatterns,
    tolerance: float,
    min_rows: int,
    min_ratio: float,
) -> list[Column]:
    """Cluster right edges of numbers; keep clusters on >= min_rows lines and >= min_ratio of the
    best-supported cluster (stray numbers in labels form small clusters)."""
    edges = sorted(w.x1 for line in lines for w in numeric_words(line, patterns))
    clusters: list[list[float]] = []
    for edge in edges:
        if clusters and edge - clusters[-1][-1] <= tolerance:
            clusters[-1].append(edge)
        else:
            clusters.append([edge])
    clusters = [part for cluster in clusters for part in _split_wide(cluster, tolerance, min_rows)]
    best = max((len(c) for c in clusters), default=0)
    floor = max(min_rows, min_ratio * best)
    return [Column(statistics.median(c), len(c)) for c in clusters if len(c) >= floor]


def _split_wide(cluster: list[float], tolerance: float, min_rows: int) -> list[list[float]]:
    """A chain of edges wider than two tolerances is several columns whose stray edges touch.

    Narrow columns (a quarter and a year column 36 pt apart) are joined into one chain by the
    numbers between them. Cut the chain at its density peaks: the densest edge position (within
    half a tolerance) that has at least `min_rows` edges and lies more than a tolerance from a
    peak already taken is a column; every edge goes to the nearest peak within a tolerance and
    the rest are dropped as stray.
    """
    if cluster[-1] - cluster[0] <= 2 * tolerance:
        return [cluster]

    def density(centre: float) -> int:
        return sum(1 for e in cluster if abs(e - centre) <= tolerance / 2)

    peaks: list[float] = []
    for centre in sorted(set(cluster), key=lambda e: (-density(e), e)):
        if density(centre) >= min_rows and all(abs(centre - p) > tolerance for p in peaks):
            peaks.append(centre)
    if len(peaks) < 2:
        return [cluster]
    parts: list[list[float]] = [[] for _ in peaks]
    for edge in cluster:
        nearest = min(range(len(peaks)), key=lambda i: abs(edge - peaks[i]))
        if abs(edge - peaks[nearest]) <= tolerance:
            parts[nearest].append(edge)
    return sorted((part for part in parts if part), key=lambda part: part[0])


def _column_of(word: Word, columns: Sequence[Column], tolerance: float) -> int | None:
    """Index of the column a (numeric) word belongs to: nearest right edge within tolerance."""
    best = min(range(len(columns)), key=lambda i: abs(word.x1 - columns[i].right), default=None)
    if best is not None and abs(word.x1 - columns[best].right) <= tolerance:
        return best
    return None


def _starts_lower(text: str) -> bool:
    """True if the first letter of the text is lower case."""
    first = next((c for c in text if c.isalpha()), "")
    return first.islower()


def _ends_open(text: str, connectors: frozenset[str]) -> bool:
    """True if the label text ends in a word or mark that says it continues on the next line."""
    stripped = text.rstrip()
    if not stripped:
        return False
    if stripped[-1] in ",-(":
        return True
    last = stripped.split()[-1].lower().rstrip(",;:-")
    return last in connectors


def _join(parts: Sequence[str]) -> str:
    """Label fragments -> one label with single spaces."""
    return " ".join(" ".join(p.split()) for p in parts if p.strip())


def _trim_footer(table: Table, needed: int) -> None:
    """Drop trailing sparse rows far below the table (page numbers, stray figures)."""
    if len(table.rows) < 3:
        return
    pitch = statistics.median(b.y - a.y for a, b in zip(table.rows, table.rows[1:], strict=False))
    while len(table.rows) > 1:
        last, before = table.rows[-1], table.rows[-2]
        if len(last.cells) >= needed or last.y - before.y <= 3 * pitch:
            break
        table.rows.pop()
        table.row_cell_counts.pop()
        table.numeric_words -= len(last.cells)


@dataclass(frozen=True)
class TableParams:
    """Geometry tunables (all from settings.parsing.pdf)."""

    row_tolerance: float
    column_tolerance: float
    min_rows: int
    header_max_lines: int
    min_support_ratio: float


def _place_line(
    line: Line, patterns: SectionPatterns, columns: Sequence[Column], tolerance: float
) -> tuple[str, dict[int, str], float | None]:
    """A line -> (label, cells by column index, left edge of its leftmost cell).

    A second number in one column stays part of the label.
    """
    numeric = {id(w) for w in numeric_words(line, patterns)}
    cells: dict[int, str] = {}
    label_words: list[Word] = []
    left: float | None = None
    for word in line.words:
        placeable = id(word) in numeric or _NOTE_REF.match(word.text)
        index = _column_of(word, columns, tolerance) if placeable else None
        if index is None or index in cells:
            label_words.append(word)
        else:
            cells[index] = word.text
            left = word.x0 if left is None else min(left, word.x0)
    return _join([w.text for w in label_words]), cells, left


def build_table(
    words: Sequence[Word], patterns: SectionPatterns, params: TableParams
) -> Table | None:
    """Positioned words -> Table, or None if no column has numbers on at least min_rows lines."""
    lines = group_lines(words, params.row_tolerance)
    columns = find_columns(
        lines, patterns, params.column_tolerance, params.min_rows, params.min_support_ratio
    )
    if not columns:
        return None
    placed = [
        (line, *_place_line(line, patterns, columns, params.column_tolerance)) for line in lines
    ]
    # the numeric zone starts at the leftmost placed number (labels never reach into it)
    left_edge = min(left for _, _, _, left in placed if left is not None) - params.column_tolerance
    needed = max(1, -(-len(columns) // 2))  # the first data row fills at least half the columns
    candidates = [i for i, (_, _, cells, _) in enumerate(placed) if len(cells) >= needed]
    if not candidates:
        return None
    # a line that carries a date is a header line even when a year in it looks like a number
    candidates = [
        i for i in candidates if not date_spans(placed[i][0].text, patterns)
    ] or candidates
    # the table starts below the lines that carry its period dates; a stray number above them
    # (a postcode in the address block) is not a data row
    first = next(
        (
            i
            for i in candidates
            if any(
                date_spans(ln.text, patterns)
                for ln, _, _, _ in placed[max(0, i - params.header_max_lines) : i]
            )
        ),
        candidates[0],
    )
    table = Table(
        columns, [], [ln for ln, _, _, _ in placed[max(0, first - params.header_max_lines) : first]]
    )
    connectors = patterns.continuation_words
    pending: list[str] = []  # label-only lines waiting for a numeric row
    for number, (line, label, cells, _) in enumerate(placed[first:], start=first + 1):
        zone = sum(1 for w in line.words if _center(w) >= left_edge) if cells else 0
        if not cells:
            if table.rows and not pending and _starts_lower(label):
                last = table.rows[-1]  # label wraps downward: this line finishes the row above
                table.rows[-1] = Row(last.index, _join([last.label, label]), last.cells, last.y)
            elif label:
                pending.append(label)
            continue
        last = table.rows[-1] if table.rows else None
        if (
            not label
            and not pending
            and last
            and _ends_open(last.label, connectors)
            and not (set(cells) & set(last.cells))
        ):  # numbers centred between the lines of a wrapped label: same row
            table.rows[-1] = Row(last.index, last.label, {**last.cells, **cells}, last.y)
            table.row_cell_counts[-1] = len(table.rows[-1].cells)
            table.numeric_words += len(cells)
            table.zone_words += zone
            continue
        head: list[str] = []
        if not label and pending:  # no label of its own: take the nearest label line above
            label = pending.pop()
        if pending and (_starts_lower(label) or _ends_open(pending[-1], connectors)):
            head.insert(0, pending.pop())
        while pending and head and _ends_open(pending[-1], connectors):
            head.insert(0, pending.pop())
        pending.clear()
        table.rows.append(Row(number, _join([*head, label]), cells, line.center))
        table.numeric_words += len(cells)
        table.zone_words += zone
        table.row_cell_counts.append(len(cells))
    _trim_footer(table, needed)
    return table if len(table.rows) >= params.min_rows else None
