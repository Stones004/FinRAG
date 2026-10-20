"""Versioned prompt templates: config/prompts/<version>/<name>.txt with {placeholders}."""

from __future__ import annotations

import re
from collections.abc import Mapping

from finrag.core.config import ConfigError, get_settings

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def load_prompt(name: str, version: str | None = None) -> str:
    """Prompt name (and version, default llm.prompt_version) -> template text."""
    settings = get_settings()
    path = settings.paths.prompts / (version or settings.llm.prompt_version) / f"{name}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"prompt template not found: {path}") from exc


def placeholders(template: str) -> set[str]:
    """Template -> the names of its {placeholders}."""
    return set(_PLACEHOLDER.findall(template))


def render(template: str, values: Mapping[str, str]) -> str:
    """Fill every {placeholder} from `values`; other braces (JSON examples) are left alone."""
    missing = placeholders(template) - set(values)
    if missing:
        raise ValueError(f"missing prompt values: {', '.join(sorted(missing))}")
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template)
