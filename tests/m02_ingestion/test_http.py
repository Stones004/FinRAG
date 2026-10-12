import httpx
import pytest

from finrag.core.config import ConfigError, get_settings
from finrag.ingestion.http import HttpError, HttpxClient, RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_limiter_allows_the_limit_then_waits_for_the_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, clock, clock.sleep)
    for _ in range(3):
        limiter.wait()
    assert clock.slept == []
    limiter.wait()  # the 4th inside one minute must wait for the 1st to age out
    assert clock.slept == [60.0] and clock.now == 60.0


def test_limiter_counts_a_sliding_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, clock, clock.sleep)
    limiter.wait()
    clock.now = 30.0
    limiter.wait()
    clock.now = 70.0  # first call is 70s old: a slot is free
    limiter.wait()
    assert clock.slept == []
    limiter.wait()  # window now holds t=30 and t=70: wait until 90
    assert clock.slept == [20.0]


def test_configured_requests_per_minute_is_enforced() -> None:
    limit = get_settings().sources.nse.requests_per_minute
    clock = FakeClock()
    limiter = RateLimiter(limit, clock, clock.sleep)
    for _ in range(limit):
        limiter.wait()
    assert clock.slept == [] and clock.now == 0.0
    limiter.wait()  # request number limit + 1 within the minute
    assert clock.slept == [60.0]


def test_limiter_rejects_a_zero_limit() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)


def make_client(handler, base_url="https://example.test", retries=2):
    nse = get_settings().sources.nse.model_copy(
        update={"base_url": base_url, "retries": retries, "backoff_s": 5, "user_agent": "t"}
    )
    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    http = HttpxClient(nse, client, RateLimiter(1000, clock, clock.sleep), clock.sleep)
    return http, clock


def test_warm_up_then_json() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    http, _ = make_client(handler)
    assert http.get_json("/api/x", {"a": "1"}) == {"ok": True}
    assert http.get_json("/api/y", {}) == {"ok": True}
    assert seen == [
        "https://example.test",
        "https://example.test/api/x?a=1",
        "https://example.test/api/y",
    ]


def test_retries_with_exponential_backoff_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200)
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, content=b"data")

    http, clock = make_client(handler)
    assert http.get_bytes("https://example.test/file.pdf") == b"data"
    assert clock.slept == [5, 10]


def test_gives_up_after_the_configured_attempts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200) if request.url.path == "/" else httpx.Response(500)

    http, _ = make_client(handler, retries=1)
    with pytest.raises(HttpError, match="2 attempts"):
        http.get_bytes("https://example.test/file.pdf")


def test_transport_errors_are_retried() -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200)
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, content=b"ok")

    http, _ = make_client(handler)
    assert http.get_bytes("https://example.test/f") == b"ok"


def test_blocked_is_reported_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403)

    http, clock = make_client(handler)
    with pytest.raises(HttpError, match="403"):
        http.get_json("/api/x", {})
    assert calls["n"] == 1 and clock.slept == []


def test_not_found_is_an_error_without_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200) if request.url.path == "/" else httpx.Response(404)

    http, clock = make_client(handler)
    with pytest.raises(HttpError, match="404"):
        http.get_bytes("https://example.test/missing")
    assert clock.slept == []


def test_non_json_body_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    http, _ = make_client(handler)
    with pytest.raises(HttpError, match="JSON"):
        http.get_json("/api/x", {})


def test_blank_base_url_is_a_config_error() -> None:
    http, _ = make_client(lambda r: httpx.Response(200), base_url="")
    with pytest.raises(ConfigError, match="base_url"):
        http.get_json("/api/x", {})
