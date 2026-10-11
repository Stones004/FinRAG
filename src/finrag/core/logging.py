"""Logging: JSON-lines file handler, readable console handler, bound run_id/isin context."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from finrag.core.config import Settings, get_settings

ROOT_LOGGER = "finrag"
LOG_FILE_NAME = "finrag.log"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_HANDLER_MARK = "_finrag_handler"
_STANDARD_KEYS = ("run_id", "isin")

_context: ContextVar[dict[str, Any] | None] = ContextVar("finrag_log_context", default=None)


class JsonFormatter(logging.Formatter):
    """Format a record as one JSON line with run_id, module, isin, event and level."""

    def format(self, record: logging.LogRecord) -> str:
        context = _context.get() or {}
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "module": record.name.removeprefix(f"{ROOT_LOGGER}."),
            "event": getattr(record, "event", None) or record.getMessage(),
            **{key: context.get(key) for key in _STANDARD_KEYS},
            **{k: v for k, v in context.items() if k not in _STANDARD_KEYS},
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def get_logger(module: str) -> logging.Logger:
    """Module name -> logger under the 'finrag' namespace."""
    return logging.getLogger(f"{ROOT_LOGGER}.{module}")


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Attach fields (e.g. run_id, isin) to every log line emitted inside the block."""
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def reset_logging() -> None:
    """Close and remove the handlers installed by configure_logging."""
    logger = logging.getLogger(ROOT_LOGGER)
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            logger.removeHandler(handler)
            handler.close()


def configure_logging(settings: Settings | None = None) -> None:
    """Install the file and console handlers on the 'finrag' logger (idempotent)."""
    settings = settings or get_settings()
    reset_logging()
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(settings.app.log_level)
    logger.propagate = False

    settings.paths.logs.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(settings.paths.logs / LOG_FILE_NAME, encoding="utf-8")
    file_handler.setFormatter(
        JsonFormatter() if settings.logging.json_logs else logging.Formatter(_CONSOLE_FORMAT)
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    for handler in (file_handler, console_handler):
        setattr(handler, _HANDLER_MARK, True)
        logger.addHandler(handler)
