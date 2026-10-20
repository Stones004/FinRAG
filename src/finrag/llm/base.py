"""The LLM client protocol, its response type and the failure callers must handle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LLMUnavailable(Exception):
    """The model server cannot give a usable answer (down, timed out, or invalid output)."""


class LLMInvalidOutput(LLMUnavailable):
    """The server answered, but not with JSON that fits the requested schema (twice)."""


@dataclass(frozen=True)
class LLMResponse:
    """What came back from one generate() call.

    `data` is the parsed and schema-checked JSON when a schema was requested, otherwise None.
    """

    text: str
    model: str
    prompt_version: str
    latency_ms: int
    data: Any = None


class LLMClient(Protocol):
    """Anything that can turn a system + user prompt into text (mockable in tests)."""

    def generate(
        self, system: str, prompt: str, json_schema: dict[str, Any] | None = None
    ) -> LLMResponse:
        """Prompts (and an optional JSON schema) -> LLMResponse; may raise LLMUnavailable."""
        ...

    def ping(self) -> bool:
        """True if the server answers and the configured model is available."""
        ...
