"""A deterministic LLM client for tests: canned replies, no network."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from finrag.llm.base import LLMInvalidOutput, LLMResponse, LLMUnavailable
from finrag.llm.schema import schema_errors


def prompt_key(system: str, prompt: str) -> str:
    """System + user prompt -> the hash FakeLLM looks replies up by."""
    return hashlib.sha256(f"{system}\n{prompt}".encode()).hexdigest()[:16]


class FakeLLM:
    """Replies by prompt hash, then by substring rules, then by `default`; records every call.

    `available=False` behaves like a server that is down. `reply` may be a callable
    (system, prompt) -> text for tests that need to compute the answer from the prompt.
    """

    model = "fake-llm"
    prompt_version = "test"

    def __init__(
        self,
        canned: Mapping[str, str] | None = None,
        rules: list[tuple[str, str]] | None = None,
        default: str | None = None,
        reply: Callable[[str, str], str] | None = None,
        available: bool = True,
    ) -> None:
        self.canned = dict(canned or {})
        self.rules = list(rules or [])
        self.default = default
        self.reply = reply
        self.available = available
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def ping(self) -> bool:
        """True unless `available` is False."""
        return self.available

    def generate(
        self, system: str, prompt: str, json_schema: dict[str, Any] | None = None
    ) -> LLMResponse:
        """Canned reply (JSON-checked against the schema if one is given) or LLMUnavailable."""
        self.calls.append((system, prompt, json_schema))
        if not self.available:
            raise LLMUnavailable("fake server is down")
        text = self._text(system, prompt)
        data = None
        if json_schema is not None:
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise LLMInvalidOutput(f"not JSON: {exc}") from exc
            problems = schema_errors(data, json_schema)
            if problems:
                raise LLMInvalidOutput("; ".join(problems))
        return LLMResponse(
            text=text, model=self.model, prompt_version=self.prompt_version, latency_ms=0, data=data
        )

    def _text(self, system: str, prompt: str) -> str:
        """Pick the reply for a prompt."""
        key = prompt_key(system, prompt)
        if key in self.canned:
            return self.canned[key]
        for needle, text in self.rules:
            if needle in prompt:
                return text
        if self.reply is not None:
            return self.reply(system, prompt)
        if self.default is not None:
            return self.default
        raise LLMUnavailable(f"no canned reply for prompt {key}")
