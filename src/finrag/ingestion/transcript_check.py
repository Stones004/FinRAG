"""Detect link-only transcript cover letters and read the quarter a transcript names."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from finrag.core.config import ConfigError, Settings, get_settings, load_registry
from finrag.core.fiscal import fy_label

_QUARTER = r"Q(?P<q>[1-4])\b[^\n]{0,30}?FY\s*['’]?\s*(?P<y>\d{4}|\d{2})\b"
_SUBJECT_RE = re.compile(_QUARTER, re.IGNORECASE)
_TITLE_RE = re.compile(
    rf"^[^\n]*?{_QUARTER}[^\n]*?Earnings\s+Conference\s+Call\W*$", re.IGNORECASE | re.MULTILINE
)
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+", re.IGNORECASE)
_LINK_WORDS = re.compile(r"\b(link|website|www|click|available at|webcast)\b", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:"
_MAX_LINK_LINES = 5
_CENTURY = 2000


class TranscriptCheckError(Exception):
    """Raised when the PDF cannot be read."""


@dataclass(frozen=True)
class TranscriptCheck:
    """What the cover letter and title block of a transcript PDF say."""

    is_link_only: bool
    links: tuple[str, ...] = ()
    link_text: tuple[str, ...] = field(default_factory=tuple)
    subject_quarter: str | None = None  # from the cover letter; unverified
    title_quarter: str | None = None  # from the "Earnings Conference Call" title line
    page_count: int = 0


def qa_start_pattern() -> re.Pattern[str]:
    """Compile transcript_markers.qa_start from the sections registry."""
    markers = load_registry("sections").get("transcript_markers")
    pattern = markers.get("qa_start") if isinstance(markers, dict) else None
    if not isinstance(pattern, str) or not pattern:
        raise ConfigError("sections registry: transcript_markers.qa_start is missing")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(
            f"sections registry: transcript_markers.qa_start is not a regex: {exc}"
        ) from exc


def _quarter_label(match: re.Match[str]) -> str:
    """Quarter regex match -> 'Q1 FY27' (label format from fiscal settings)."""
    year = int(match["y"])
    year = year if year >= _CENTURY else _CENTURY + year
    return f"Q{match['q']} {fy_label(year)}"


def _clean_url(url: str) -> str:
    """Strip trailing punctuation that is not part of the URL."""
    return url.rstrip(_TRAILING_PUNCT)


def _find_links(pages: list[str], uris: list[str]) -> tuple[str, ...]:
    """Page text + link annotations -> distinct URLs in first-seen order."""
    found = [_clean_url(u) for u in uris]
    for text in pages:
        found += [_clean_url(m) for m in _URL_RE.findall(text)]
    return tuple(dict.fromkeys(u for u in found if u))


def _link_lines(text: str) -> tuple[str, ...]:
    """Cover-letter text -> the lines that mention a link or website."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    hits = [line for line in lines if line and (_URL_RE.search(line) or _LINK_WORDS.search(line))]
    return tuple(hits[:_MAX_LINK_LINES])


def _title_quarter(pages: list[str], scan_pages: int) -> str | None:
    """Last standalone '<Qn FYyy> Earnings Conference Call' line in the first pages -> label."""
    text = "\n".join(pages[:scan_pages])
    matches = list(_TITLE_RE.finditer(text))
    return _quarter_label(matches[-1]) if matches else None


def check_pages(
    pages: list[str], uris: list[str] | None = None, settings: Settings | None = None
) -> TranscriptCheck:
    """Page texts (+ link annotations) -> TranscriptCheck."""
    settings = settings or get_settings()
    max_pages = settings.ingestion.transcript_link_only_max_pages
    normalised = " ".join(" ".join(pages).split())
    has_qa = qa_start_pattern().search(normalised) is not None
    subject = _SUBJECT_RE.search(pages[0]) if pages else None
    link_only = len(pages) <= max_pages and not has_qa
    return TranscriptCheck(
        is_link_only=link_only,
        links=_find_links(pages, uris or []) if link_only else (),
        link_text=_link_lines("\n".join(pages)) if link_only else (),
        subject_quarter=_quarter_label(subject) if subject else None,
        title_quarter=_title_quarter(pages, max_pages + 1),
        page_count=len(pages),
    )


def check_transcript(data: bytes, settings: Settings | None = None) -> TranscriptCheck:
    """Transcript PDF bytes -> TranscriptCheck (text layer only, no OCR)."""
    import pymupdf

    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            pages = [page.get_text() for page in doc]
            uris = [link["uri"] for page in doc for link in page.get_links() if link.get("uri")]
    except Exception as exc:  # pymupdf raises several error types for bad files
        raise TranscriptCheckError(f"cannot read PDF: {exc}") from exc
    return check_pages(pages, uris, settings)
