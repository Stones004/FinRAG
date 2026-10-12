"""The filing time the exchange puts in XBRL file names: `..._DDMMYYYYhhmmss_...`."""

from __future__ import annotations

import re
from datetime import datetime

_STAMP = re.compile(r"_(\d{2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})_")


def filename_timestamp(name: str) -> datetime | None:
    """File name or URL -> the first valid _DDMMYYYYhhmmss_ time in it, or None."""
    for match in _STAMP.finditer(name):
        day, month, year, hour, minute, second = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            continue
    return None
