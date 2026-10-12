"""HTTP access for exchange adapters: rate limit, retries with backoff, timeouts, user agent."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from finrag.core.config import ConfigError, SourceEndpointSettings
from finrag.core.logging import get_logger

log = get_logger("ingestion")

_WINDOW_S = 60.0
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_BLOCKED_STATUS = frozenset({401, 403})


class HttpError(Exception):
    """Raised when a request fails for good; the message says what to check."""


class RateLimiter:
    """At most `requests_per_minute` calls to `wait()` return in any 60-second window."""

    def __init__(
        self,
        requests_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self._limit = requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()

    def wait(self) -> None:
        """Block (via the injected sleep) until a request may be sent, then record it."""
        now = self._clock()
        while self._sent and now - self._sent[0] >= _WINDOW_S:
            self._sent.popleft()
        if len(self._sent) >= self._limit:
            self._sleep(self._sent[0] + _WINDOW_S - now)
            now = self._clock()
            while self._sent and now - self._sent[0] >= _WINDOW_S:
                self._sent.popleft()
        self._sent.append(now)


class HttpClient(Protocol):
    """What exchange adapters need from HTTP."""

    def get_json(self, path: str, params: dict[str, str]) -> Any:
        """Endpoint path (joined to base_url) + query -> decoded JSON."""

    def get_bytes(self, url: str) -> bytes:
        """Absolute URL -> response body."""


class HttpxClient:
    """HttpClient over httpx with config-driven limits.

    The first request visits base_url to pick up session cookies. UNVERIFIED against the live
    site: NSE's cookie requirements were only observed from a browser page (PROGRESS finding 1).
    """

    def __init__(
        self,
        settings: SourceEndpointSettings,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        headers = {"User-Agent": settings.user_agent} if settings.user_agent else {}
        self._client = client or httpx.Client(
            timeout=settings.timeout_s, headers=headers, follow_redirects=True
        )
        self._limiter = limiter or RateLimiter(settings.requests_per_minute)
        self._sleep = sleep
        self._warmed_up = False

    def _base_url(self) -> str:
        """The configured base URL (blank means access has not been set up)."""
        if not self._settings.base_url:
            raise ConfigError("base_url is blank in the settings for this source: fill it in first")
        return self._settings.base_url.rstrip("/")

    def _send(self, url: str, params: dict[str, str] | None) -> httpx.Response:
        """GET with rate limiting, retries and backoff; blocked/other errors raise HttpError."""
        last = "no attempt made"
        for attempt in range(self._settings.retries + 1):
            self._limiter.wait()
            try:
                response = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code in _BLOCKED_STATUS:
                    raise HttpError(
                        f"{url} refused with HTTP {response.status_code}: the site may require "
                        "browser cookies or disallow automated access"
                    )
                if response.status_code not in _RETRY_STATUS:
                    if response.is_error:
                        raise HttpError(f"{url} failed with HTTP {response.status_code}")
                    return response
                last = f"HTTP {response.status_code}"
            if attempt < self._settings.retries:
                self._sleep(self._settings.backoff_s * 2**attempt)
        raise HttpError(f"{url} failed after {self._settings.retries + 1} attempts ({last})")

    def _warm_up(self) -> None:
        """Visit the base URL once so the session holds the site's cookies."""
        if not self._warmed_up:
            self._send(self._base_url(), None)
            self._warmed_up = True

    def get_json(self, path: str, params: dict[str, str]) -> Any:
        """Endpoint path + query -> decoded JSON."""
        self._warm_up()
        response = self._send(self._base_url() + path, params)
        try:
            return response.json()
        except ValueError as exc:
            raise HttpError(f"{path} did not return JSON") from exc

    def get_bytes(self, url: str) -> bytes:
        """Absolute URL -> response body."""
        self._warm_up()
        return self._send(url, None).content
