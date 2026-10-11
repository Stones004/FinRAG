"""ID helpers: time-sortable ULIDs, deterministic filing IDs, chunk IDs."""

from __future__ import annotations

import re
import secrets
import threading
import time

PREFIX_FACT = "FCT"
PREFIX_METRIC = "MET"
PREFIX_QUERY = "QRY"
PREFIX_JOB = "JOB"
PREFIX_RUN = "RUN"
PREFIX_ALERT = "ALR"
PREFIX_SIGNAL = "SIG"
PREFIX_FILING = "FIL"
PREFIX_CHUNK = "CHK"

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
_RANDOM_BITS = 80
_FILING_HEX_CHARS = 12
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _now_ms() -> int:
    """Return the current time in milliseconds."""
    return time.time_ns() // 1_000_000


class _UlidGenerator:
    """Monotonic ULID source: IDs from one process always sort in creation order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_random = 0

    def next(self) -> str:
        """Return a new 26-character ULID."""
        with self._lock:
            ms = _now_ms()
            if ms <= self._last_ms:  # same millisecond, or the clock stepped back
                ms = self._last_ms
                rand = self._last_random + 1
                if rand >> _RANDOM_BITS:
                    ms += 1
                    rand = secrets.randbits(_RANDOM_BITS)
            else:
                rand = secrets.randbits(_RANDOM_BITS)
            self._last_ms, self._last_random = ms, rand
        value = (ms << _RANDOM_BITS) | rand
        chars = []
        for _ in range(26):
            chars.append(_ALPHABET[value & 31])
            value >>= 5
        return "".join(reversed(chars))


_generator = _UlidGenerator()


def new_ulid() -> str:
    """Return a new time-sortable ULID string."""
    return _generator.next()


def new_id(prefix: str) -> str:
    """Prefix -> '<prefix>-<ulid>'."""
    if not prefix:
        raise ValueError("prefix must not be empty")
    return f"{prefix}-{new_ulid()}"


def filing_id_from_sha(sha: str) -> str:
    """Full sha256 hex digest -> 'FIL-<first 12 hex chars>' (deterministic)."""
    if not _SHA256_RE.match(sha):
        raise ValueError("expected a 64-character sha256 hex digest")
    return f"{PREFIX_FILING}-{sha[:_FILING_HEX_CHARS].lower()}"


def chunk_id(filing_id: str, page: int, seq: int) -> str:
    """Filing ID, page, sequence -> 'CHK-<filing_id>-p<page>-<seq>'."""
    return f"{PREFIX_CHUNK}-{filing_id}-p{page}-{seq}"
