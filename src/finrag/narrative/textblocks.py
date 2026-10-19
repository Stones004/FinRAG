"""XBRL text blocks of results filings: the four indexed concepts from text_facts.jsonl."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from pathlib import Path

from finrag.narrative.models import TextBlock

_BREAK = re.compile(r"(?i)<\s*br\s*/?\s*>")
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"[ \t\r\f\v]*\n[ \t\r\f\v\n]*")
_SPACES = re.compile(r"[ \t\r\f\v]+")


def clean_block(value: str) -> str:
    """Text-block value -> plain text: <BR> becomes a line break, other tags and entities go."""
    text = html.unescape(_TAG.sub(" ", _BREAK.sub("\n", value)))
    text = _SPACES.sub(" ", text)
    return _BLANK_LINES.sub("\n", text).strip()


def read_text_blocks(path: Path, concepts: Mapping[str, str]) -> list[TextBlock]:
    """text_facts.jsonl -> the indexed blocks (entity totals only, non-empty, no duplicates).

    A concept that is absent in the filing is simply absent; blocks with dimensions (member
    tables, related-party rows) are never indexed.
    """
    if not path.exists():
        return []
    seen: set[tuple[str, str]] = set()
    blocks: list[TextBlock] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            fact = json.loads(line)
            section = concepts.get(fact.get("concept", ""))
            if section is None or fact.get("dimensions"):
                continue
            text = clean_block(str(fact.get("value") or ""))
            key = (fact["concept"], text)
            if not text or key in seen:
                continue
            seen.add(key)
            blocks.append(TextBlock(fact["concept"], section, text))
    return blocks
