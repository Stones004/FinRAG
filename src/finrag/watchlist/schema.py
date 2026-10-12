"""Pydantic models for the watch list file and for an Entity record."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from finrag.core.config import ConfigError, get_settings, load_registry

EntityStatus = Literal["active", "paused"]
BasisChoice = Literal["consolidated", "standalone"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WatchlistCompany(_Model):
    """One watch list entry: an identifier plus optional overrides."""

    id: str
    sector_type: str | None = None
    basis: BasisChoice | None = None
    status: EntityStatus = "active"

    @field_validator("id", mode="before")
    @classmethod
    def _id_as_text(cls, value: Any) -> str:
        """Keep an unquoted numeric id (a BSE code) as text; reject blanks and non-text ids."""
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("id must be an NSE symbol, BSE code, ISIN or company name")
        text = str(value).strip()
        if not text:
            raise ValueError("id must not be blank")
        return text

    @field_validator("sector_type")
    @classmethod
    def _sector_allowed(cls, value: str | None) -> str | None:
        """sector_type must be one of settings.watchlist.allowed_sector_types."""
        allowed = get_settings().watchlist.allowed_sector_types
        if value is not None and value not in allowed:
            raise ValueError(f"sector_type '{value}' is not allowed; use one of {allowed}")
        return value


class WatchlistFile(_Model):
    """The whole watch list file."""

    companies: list[WatchlistCompany] = []


class Entity(_Model):
    """One row of the entities table."""

    isin: str
    name: str | None = None
    nse_symbol: str | None = None
    bse_code: str | None = None
    sector_type: str
    default_basis: BasisChoice
    status: EntityStatus
    added_on: str
    updated_on: str


def parse_watchlist(data: dict[str, Any]) -> WatchlistFile:
    """Raw mapping -> WatchlistFile; ConfigError names the offending entry and field."""
    if data.get("companies") is None:
        data = {**data, "companies": []}
    try:
        return WatchlistFile.model_validate(data)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            where = ".".join(str(part) for part in err["loc"]) or "watchlist"
            lines.append(f"{where}: {err['msg']}")
        raise ConfigError("invalid watchlist:\n  " + "\n  ".join(lines)) from exc


def load_watchlist() -> WatchlistFile:
    """Read and validate the watch list file (path from settings.paths.watchlist)."""
    return parse_watchlist(load_registry("watchlist"))
