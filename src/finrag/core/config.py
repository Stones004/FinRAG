"""Load and validate config/settings.yaml; load registry YAMLs by name."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"

ENV_PREFIX = "FINRAG_"
ENV_SEPARATOR = "__"
# Env overrides are allowed only for these dotted keys (secrets); settings.yaml is the source.
ENV_SECRET_KEYS = frozenset({"alerts.email.password"})
MASK = "***"

REGISTRY_NAMES = (
    "watchlist",
    "taxonomy",
    "metrics",
    "validation_rules",
    "sections",
    "intents",
)


class ConfigError(Exception):
    """Raised for missing/invalid configuration; the message names the offending key."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- sections -------------------------------------------------------------------------------


class AppSettings(_Model):
    name: str
    timezone: str
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class PathsSettings(_Model):
    data_dir: Path
    db: Path
    raw_store: Path
    processed: Path
    indexes: Path
    reference: Path
    inbox: Path
    inbox_processed: Path
    logs: Path
    watchlist: Path
    taxonomy: Path
    metrics: Path
    validation_rules: Path
    sections: Path
    intents: Path
    prompts: Path
    vocab_common_en: Path

    @field_validator("*", mode="after")
    @classmethod
    def _resolve(cls, value: Path, info: ValidationInfo) -> Path:
        """Resolve a relative path against the repo root given in the validation context."""
        root = Path((info.context or {}).get("root", REPO_ROOT))
        return value if value.is_absolute() else Path(os.path.normpath(root / value))

    def data_folders(self) -> list[Path]:
        """Return the data folders `finrag init` creates (not files, not config folders)."""
        return [
            self.data_dir,
            self.raw_store,
            self.processed,
            self.indexes,
            self.reference,
            self.inbox,
            self.logs,
        ]


class FiscalSettings(_Model):
    fy_start_month: int = Field(ge=1, le=12)
    fy_label_format: str

    @field_validator("fy_label_format")
    @classmethod
    def _has_year_token(cls, value: str) -> str:
        """Require exactly one {yy} token and no other braces."""
        if (
            value.count("{yy}") != 1
            or value.replace("{yy}", "").count("{")
            or "}" in value.replace("{yy}", "")
        ):
            raise ValueError("must contain exactly one '{yy}' token and no other braces")
        return value


class HistorySettings(_Model):
    years_back: int = Field(ge=1)


class DefaultsSettings(_Model):
    basis: Literal["consolidated", "standalone"]
    currency: str
    display_unit: str
    display_decimals: int = Field(ge=0)


class UnitsSettings(_Model):
    multipliers: dict[str, int]

    @field_validator("multipliers")
    @classmethod
    def _positive(cls, value: dict[str, int]) -> dict[str, int]:
        """Reject non-positive multipliers."""
        if any(v <= 0 for v in value.values()):
            raise ValueError("multipliers must be positive")
        return value


class WatchlistSettings(_Model):
    default_sector_type: str
    allowed_sector_types: list[str]
    name_match_min_score: int = Field(ge=0, le=100)
    suggest_max: int = Field(ge=0)
    suggest_min_score: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _default_allowed(self) -> WatchlistSettings:
        """Default sector type must be one of the allowed types."""
        if self.default_sector_type not in self.allowed_sector_types:
            raise ValueError("default_sector_type must be in allowed_sector_types")
        return self


class SecuritiesMasterSettings(_Model):
    nse_file: str
    bse_file: str


class SourceEndpointSettings(_Model):
    base_url: str
    requests_per_minute: int = Field(gt=0)
    timeout_s: int = Field(gt=0)
    retries: int = Field(ge=0)
    backoff_s: int = Field(ge=0)
    user_agent: str


class NseEndpoints(_Model):
    integrated_filing_results: str
    legacy_financial_results: str
    annual_reports: str
    corporate_announcements: str


class NseSettings(SourceEndpointSettings):
    endpoints: NseEndpoints
    integrated_filing_type: str
    integrated_from_period_end: date
    legacy_result_periods: list[str]
    transcript_desc: str
    transcript_text_marker: str
    announcement_window_days: int = Field(gt=0)


class SourcesSettings(_Model):
    enabled: list[str]
    securities_master: SecuritiesMasterSettings
    nse: NseSettings
    bse: SourceEndpointSettings
    filing_types: list[str]


class IngestionSettings(_Model):
    transcript_link_only_max_pages: int = Field(ge=0)


class ChandraSettings(_Model):
    base_url: str
    model: str
    timeout_s: int = Field(gt=0)
    max_image_px: int = Field(gt=0)


class OcrSettings(_Model):
    ocr_enabled: bool
    ocr_lang: str
    ocr_dpi: int = Field(gt=0)
    backend: Literal["tesseract", "chandra"]
    tesseract_cmd: str  # blank = find tesseract on PATH
    rule_min_length_pt: float = Field(
        ge=0
    )  # ruled lines at least this long are erased before OCR; 0 = off
    tesseract_passes: list[str] = Field(
        min_length=1
    )  # tesseract options per pass; see ocr/tesseract.py
    chandra: ChandraSettings


class PdfSettings(_Model):
    min_text_chars_for_native: int = Field(ge=0)
    min_text_quality: float = Field(ge=0, le=1)
    min_table_confidence: float = Field(ge=0, le=1)
    unit_inherited_confidence_penalty: float = Field(
        ge=0, le=1
    )  # taken off facts using a unit from another page
    format_outlier_confidence: float = Field(ge=0, le=1)  # cap for a number with unusual decimals
    statement_page_min_score: int = Field(ge=0)
    quality_min_token_len: int = Field(ge=1)  # shortest alphabetic token the quality score counts
    row_tolerance_pt: float = Field(gt=0)  # words within this vertical distance share a table row
    column_tolerance_pt: float = Field(gt=0)  # right edges within this distance share a column
    min_table_rows: int = Field(ge=1)  # numeric rows needed before a page is read as a table
    column_min_support_ratio: float = Field(ge=0, le=1)  # weakest column vs the strongest
    header_max_lines: int = Field(
        ge=1
    )  # text lines above the first numeric row scanned for headers
    basis_scan_lines: int = Field(
        ge=1
    )  # first lines of a page searched for standalone/consolidated


class ParsingSettings(_Model):
    ocr: OcrSettings
    pdf: PdfSettings


class NormalizeSettings(_Model):
    fuzzy_suggest_min_score: int = Field(ge=0, le=100)


class ToleranceSettings(_Model):
    relative_tolerance: float = Field(ge=0)
    absolute_tolerance_inr: int = Field(ge=0)


class CrossSourceSettings(_Model):
    rounding_inr: int = Field(ge=0)
    min_shifted_keys: int = Field(gt=0)
    decimal_shift_max_power: int = Field(gt=0)


class ValidationSettings(ToleranceSettings):
    cross_source: CrossSourceSettings


class MetricsSettings(_Model):
    version_policy: Literal["latest", "pinned"]
    pinned_versions: dict[str, str]
    decimal_precision: int = Field(ge=10)  # significant digits of every metric calculation


class RetrievalSettings(_Model):
    top_k_bm25: int = Field(gt=0)
    top_k_dense: int = Field(gt=0)
    rrf_k: int = Field(gt=0)
    final_k: int = Field(gt=0)
    reranker_enabled: bool
    reranker_model: str


class NarrativeSettings(_Model):
    chunk_tokens: int = Field(gt=0)
    chunk_overlap_tokens: int = Field(ge=0)
    min_chunk_tokens: int = Field(ge=0)
    embedding_model: str
    embedding_device: Literal["cpu", "cuda"]
    embedding_batch_size: int = Field(gt=0)
    vector_backend: Literal["numpy", "faiss"]
    index_annual_reports: bool
    annual_report_sections_include: list[str]
    log_skipped_sections: bool
    retrieval: RetrievalSettings

    @model_validator(mode="after")
    def _chunk_sizes(self) -> NarrativeSettings:
        """Overlap and minimum chunk size must be smaller than the chunk size."""
        if self.chunk_overlap_tokens >= self.chunk_tokens:
            raise ValueError("chunk_overlap_tokens must be less than chunk_tokens")
        if self.min_chunk_tokens >= self.chunk_tokens:
            raise ValueError("min_chunk_tokens must be less than chunk_tokens")
        return self


class IntentSettings(_Model):
    fuzzy_min_score: int = Field(ge=0, le=100)
    shortlist_size: int = Field(gt=0)
    llm_fallback_enabled: bool
    auto_proceed_threshold: float = Field(ge=0, le=1)
    clarify_threshold: float = Field(ge=0, le=1)
    default_period: str
    multi_entity_enabled: bool

    @model_validator(mode="after")
    def _thresholds(self) -> IntentSettings:
        """The clarify threshold must not exceed the auto-proceed threshold."""
        if self.clarify_threshold > self.auto_proceed_threshold:
            raise ValueError("clarify_threshold must not exceed auto_proceed_threshold")
        return self


class AnswerSettings(_Model):
    max_facts_in_prompt: int = Field(gt=0)
    max_chunks_in_prompt: int = Field(gt=0)
    require_citation_per_sentence: bool
    numeric_check_enabled: bool


class QuerySettings(_Model):
    intent: IntentSettings
    answer: AnswerSettings


class LlmSettings(_Model):
    provider: Literal["openai_compatible"]
    base_url: str
    model: str
    temperature: float = Field(ge=0)
    num_ctx: int = Field(gt=0)
    timeout_s: int = Field(gt=0)
    retries: int = Field(ge=0)
    prompt_version: str


class SchedulerSettings(_Model):
    results_season_months: list[int]
    poll_interval_hours_season: int = Field(gt=0)
    poll_interval_hours_offseason: int = Field(gt=0)
    max_parallel_jobs: int = Field(gt=0)

    @field_validator("results_season_months")
    @classmethod
    def _months(cls, value: list[int]) -> list[int]:
        """Months must be 1..12."""
        if any(m < 1 or m > 12 for m in value):
            raise ValueError("months must be between 1 and 12")
        return value


class EmailSettings(_Model):
    smtp_host: str
    smtp_port: int = Field(gt=0)
    from_addr: str
    to_addr: str
    password: str = ""  # secret: supplied only via FINRAG_ALERTS__EMAIL__PASSWORD


class AlertsSettings(_Model):
    channels: list[Literal["log", "email"]]
    email: EmailSettings


class UiSettings(_Model):
    show_blocked_metrics: bool
    show_interpretation: bool
    default_years_shown: int = Field(gt=0)


class LoggingSettings(_Model):
    json_logs: bool
    query_log_enabled: bool


class Settings(_Model):
    app: AppSettings
    paths: PathsSettings
    fiscal: FiscalSettings
    history: HistorySettings
    defaults: DefaultsSettings
    units: UnitsSettings
    watchlist: WatchlistSettings
    sources: SourcesSettings
    ingestion: IngestionSettings
    parsing: ParsingSettings
    normalize: NormalizeSettings
    restatement: ToleranceSettings
    validation: ValidationSettings
    metrics: MetricsSettings
    narrative: NarrativeSettings
    query: QuerySettings
    llm: LlmSettings
    scheduler: SchedulerSettings
    alerts: AlertsSettings
    ui: UiSettings
    logging: LoggingSettings

    @model_validator(mode="after")
    def _display_unit_known(self) -> Settings:
        """defaults.display_unit must be a key of units.multipliers."""
        if self.defaults.display_unit not in self.units.multipliers:
            raise ValueError("defaults.display_unit must be a key of units.multipliers")
        return self


# --- loading --------------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file that must contain a mapping."""
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def _apply_env_overrides(data: dict[str, Any], environ: Mapping[str, str]) -> None:
    """Apply FINRAG_<SECTION>__<KEY> overrides for allowlisted secret keys; reject any other."""
    for name, value in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        dotted = ".".join(name[len(ENV_PREFIX) :].lower().split(ENV_SEPARATOR))
        if dotted not in ENV_SECRET_KEYS:
            raise ConfigError(
                f"environment variable {name} is not allowed: only secrets may be overridden "
                f"({', '.join(sorted(ENV_SECRET_KEYS))})"
            )
        node = data
        *parents, leaf = dotted.split(".")
        for part in parents:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigError(f"cannot apply {name}: '{part}' is not a section")
            node = child
        node[leaf] = value


def _format_errors(exc: ValidationError) -> str:
    """Render pydantic errors as '<dotted.key>: <message>' lines."""
    lines = []
    for err in exc.errors():
        key = ".".join(str(part) for part in err["loc"]) or "settings"
        lines.append(f"{key}: {err['msg']}")
    return "invalid settings:\n  " + "\n  ".join(lines)


def load_settings(
    path: Path | None = None,
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Read a settings YAML, apply env overrides, validate -> Settings (uncached)."""
    data = _read_yaml(path or SETTINGS_PATH)
    _apply_env_overrides(data, os.environ if environ is None else environ)
    try:
        return Settings.model_validate(data, context={"root": root or REPO_ROOT})
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached, validated Settings for config/settings.yaml."""
    return load_settings()


def reset_settings_cache() -> None:
    """Drop the cached Settings (used by tests)."""
    get_settings.cache_clear()


def load_registry(name: str) -> dict[str, Any]:
    """Registry name (a key under settings.paths) -> raw dict; the owning module validates it."""
    if name not in REGISTRY_NAMES:
        raise ConfigError(f"unknown registry '{name}'; expected one of {', '.join(REGISTRY_NAMES)}")
    return _read_yaml(getattr(get_settings().paths, name))


def masked_settings(settings: Settings | None = None) -> dict[str, Any]:
    """Settings -> plain dict with allowlisted secret values masked."""
    dump = (settings or get_settings()).model_dump(mode="json")
    for dotted in ENV_SECRET_KEYS:
        *parents, leaf = dotted.split(".")
        node = dump
        for part in parents:
            node = node[part]
        if node.get(leaf):
            node[leaf] = MASK
    return dump
