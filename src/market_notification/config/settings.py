"""Layered settings: defaults from config/default.toml, optional config/local.toml,
optional .env, env vars MN_* override all.

Usage:
    from market_notification.config.settings import get_settings
    settings = get_settings()
    print(settings.ollama.gemma_model)
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_TOML = CONFIG_DIR / "default.toml"
LOCAL_TOML = CONFIG_DIR / "local.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Section schemas (validated via pydantic)
# ---------------------------------------------------------------------------


class DbSettings(BaseModel):
    url: str = "sqlite:///data/notifications.db"
    wal: bool = True
    busy_timeout_ms: int = 5000


class OllamaSettings(BaseModel):
    url: str = "http://localhost:11434"
    gemma_model: str = "gemma4-zenflow-moe:latest"
    qwen_fallback_model: str = "qwen2.5:14b"
    request_timeout_s: int = 300
    keep_alive: str = "24h"


class GeminiRrSettings(BaseModel):
    binary: str = "C:/Users/user/bin/gemini-rr.cmd"
    default_timeout_s: int = 600


class TaxonomyPaths(BaseModel):
    basic_industry: str = "G:/brain/screener_util/basic_industry_taxonomy.json"
    sector_kpis: str = "D:/Annual_report_extract/docs/sector_metrics_kpis.md"
    concall_taxonomy: str = "D:/claude-codex-gemini/docs/concall_extraction_taxonomy.md"


class PathsSettings(BaseModel):
    pdf_dump_root: str = "D:/Notification Dump"
    log_dir: str = "G:/market_notification/logs"
    brain_screener_essential_db: str = "G:/brain/screener_essential.db"
    brain_notifications_db: str = "G:/brain/data/notifications.db"
    screener_original_root: str = "G:/Screener_original"
    company_sector_mapping_csv: str = (
        "G:/Screener_original/screener_util/company_sector_mapping_master.csv"
    )
    taxonomy: TaxonomyPaths = Field(default_factory=TaxonomyPaths)


class PollerSettings(BaseModel):
    interval_s: int = 60
    window_24x7: bool = True
    nse_records_per_poll: int = 50
    bse_records_per_poll: int = 100
    fetcher_request_timeout_s: int = 30


class PipelineSettings(BaseModel):
    sla_classify_minutes: int = 5
    pdf_max_pages_default: int = 20
    retry_max: int = 3
    retry_backoff_minutes: list[int] = Field(default_factory=lambda: [2, 4, 8])


class SummarizerSettings(BaseModel):
    prompt_version: str = "v1"
    batch_size: int = 1
    temperature: float = 0.1
    num_predict: int = 4096


class ClassifierSettings(BaseModel):
    prompt_version: str = "v1"
    temperature: float = 0.1
    num_predict: int = 1024


class DeepDiveSettings(BaseModel):
    prompt_version: str = "v1"
    cache_default: bool = True
    fundamentals_inject: str = "all"  # all | topic_only | none


class BackfillSettings(BaseModel):
    brain_history: bool = True
    screener_original_history: bool = True


class UiSettings(BaseModel):
    port: int = 8501
    default_show_ignored: bool = False
    list_page_size: int = 200


class HealthSettings(BaseModel):
    port: int = 8502


# ---------------------------------------------------------------------------
# Top-level Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Top-level settings. Use `get_settings()` to access."""

    model_config = SettingsConfigDict(
        env_prefix="MN_",
        env_nested_delimiter="__",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    db: DbSettings = Field(default_factory=DbSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    gemini_rr: GeminiRrSettings = Field(default_factory=GeminiRrSettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    poller: PollerSettings = Field(default_factory=PollerSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    summarizer: SummarizerSettings = Field(default_factory=SummarizerSettings)
    classifier: ClassifierSettings = Field(default_factory=ClassifierSettings)
    deep_dive: DeepDiveSettings = Field(default_factory=DeepDiveSettings)
    backfill: BackfillSettings = Field(default_factory=BackfillSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)

    debug: bool = False
    project_root: str = str(PROJECT_ROOT)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance.

    Layering order (lowest to highest precedence):
      1. config/default.toml
      2. config/local.toml (if present)
      3. .env (if present)
      4. MN_* environment variables
    """
    base = _load_toml(DEFAULT_TOML)
    local = _load_toml(LOCAL_TOML)
    merged = _deep_merge(base, local)
    return Settings(**merged)


def reload_settings() -> Settings:
    """Force-reload settings (for tests or config-edit reload)."""
    get_settings.cache_clear()
    return get_settings()
