"""Client for any OpenAI-compatible server (LM Studio by default; Ollama at its own /v1 URL)."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from finrag.core.config import LlmSettings, get_settings
from finrag.core.logging import get_logger
from finrag.llm.base import LLMInvalidOutput, LLMResponse, LLMUnavailable
from finrag.llm.schema import schema_errors

log = get_logger("llm")

_SCHEMA_NAME = "response"
_SCHEMA_HINT = (
    "\n\nReply with one JSON object only, no other text, that fits this JSON schema:\n{schema}"
)


class OpenAICompatClient:
    """POST {base_url}/chat/completions with the settings in `llm`.

    A request with a json_schema is sent with `response_format`. If the server rejects that, or the
    reply is not valid JSON for the schema, the request is retried once with the schema stated in
    the prompt; a second failure raises LLMInvalidOutput. `num_ctx` is a server-side setting for
    these servers: it is logged, not sent (TODO: confirm for LM Studio whether the API accepts it).
    """

    def __init__(
        self,
        settings: LlmSettings | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._s = settings or get_settings().llm
        self._http = httpx.Client(
            base_url=self._s.base_url.rstrip("/"),
            timeout=self._s.timeout_s,
            transport=transport,
        )
        log.info(
            "llm client",
            extra={
                "fields": {
                    "base_url": self._s.base_url,
                    "model": self._s.model,
                    "num_ctx_expected": self._s.num_ctx,
                }
            },
        )

    # -- public ------------------------------------------------------------------------------

    def ping(self) -> bool:
        """GET {base_url}/models and check that `llm.model` is listed."""
        if not self._s.model:
            log.warning("llm.model is blank")
            return False
        try:
            response = self._http.get("/models")
            response.raise_for_status()
            listed = {m.get("id") for m in response.json().get("data", [])}
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("llm ping failed", extra={"fields": {"error": str(exc)}})
            return False
        return self._s.model in listed

    def generate(
        self, system: str, prompt: str, json_schema: dict[str, Any] | None = None
    ) -> LLMResponse:
        """Prompts (and an optional JSON schema) -> LLMResponse; may raise LLMUnavailable."""
        if not self._s.model:
            raise LLMUnavailable("llm.model is blank in settings")
        started = time.monotonic()
        if json_schema is None:
            text = self._complete(system, prompt, None)
            return self._response(text, None, started)
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": _SCHEMA_NAME, "strict": True, "schema": json_schema},
        }
        try:
            text = self._complete(system, prompt, response_format)
            data = self._checked(text, json_schema)
        except _Rejected as exc:
            log.info("retrying with the schema in the prompt", extra={"fields": {"why": str(exc)}})
            hinted = prompt + _SCHEMA_HINT.format(schema=json.dumps(json_schema))
            text = self._complete(system, hinted, None)
            try:
                data = self._checked(text, json_schema)
            except _Rejected as again:
                raise LLMInvalidOutput(f"invalid JSON from the model: {again}") from again
        return self._response(text, data, started)

    # -- internals ---------------------------------------------------------------------------

    def _response(self, text: str, data: Any, started: float) -> LLMResponse:
        """Text, parsed data and start time -> LLMResponse."""
        return LLMResponse(
            text=text,
            model=self._s.model,
            prompt_version=self._s.prompt_version,
            latency_ms=int((time.monotonic() - started) * 1000),
            data=data,
        )

    @staticmethod
    def _checked(text: str, schema: dict[str, Any]) -> Any:
        """Reply text -> parsed JSON that fits the schema, or _Rejected."""
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise _Rejected(f"not JSON: {exc}") from exc
        problems = schema_errors(data, schema)
        if problems:
            raise _Rejected("; ".join(problems))
        return data

    def _complete(self, system: str, prompt: str, response_format: dict[str, Any] | None) -> str:
        """One chat completion (with connection retries) -> message text.

        A 4xx answer to a request that carried response_format means the server rejects it:
        _Rejected. Anything else that fails after the retries is LLMUnavailable.
        """
        body: dict[str, Any] = {
            "model": self._s.model,
            "temperature": self._s.temperature,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if response_format is not None:
            body["response_format"] = response_format
        last: Exception | None = None
        for attempt in range(self._s.retries + 1):
            try:
                response = self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:  # refused, timeout, reset
                last = exc
                log.warning(
                    "llm request failed", extra={"fields": {"attempt": attempt, "error": str(exc)}}
                )
                continue
            if response.status_code >= 500:
                last = LLMUnavailable(f"server error {response.status_code}")
                continue
            if 400 <= response.status_code < 500:
                if response_format is not None:
                    raise _Rejected(f"server rejected response_format ({response.status_code})")
                raise LLMUnavailable(
                    f"request rejected ({response.status_code}): {response.text[:200]}"
                )
            try:
                return str(response.json()["choices"][0]["message"]["content"] or "")
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMUnavailable(f"unreadable reply: {exc}") from exc
        raise LLMUnavailable(f"model server not reachable: {last}")


class _Rejected(Exception):
    """The reply cannot be used as asked (response_format refused, or JSON does not fit)."""
