from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _merge_unique(*values: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for items in values:
        for item in items:
            normalized = item.strip()
            key = normalized.casefold()
            if normalized and key not in seen:
                merged.append(normalized)
                seen.add(key)
    return merged


class EnvironmentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    telegram_bot_token: str
    telegram_owner_id: int
    anthropic_api_key: str | None = None
    anthropic_auth_token: str | None = None
    anthropic_base_url: str | None = None
    anthropic_model: str | None = None
    anthropic_compatibility_mode: bool = False
    tg_digest_config: Path = Path("config/groups.yaml")
    tg_digest_data_dir: Path = Path("data")
    tg_digest_log_level: str = "INFO"
    tg_digest_log_file: Path | None = None
    tg_digest_log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1_024)
    tg_digest_log_backup_count: int = Field(default=5, ge=1, le=100)
    tg_digest_log_console: bool = True

    @model_validator(mode="after")
    def validate_anthropic_credentials(self) -> EnvironmentSettings:
        self.anthropic_api_key = (self.anthropic_api_key or "").strip() or None
        self.anthropic_auth_token = (self.anthropic_auth_token or "").strip() or None
        self.anthropic_base_url = (self.anthropic_base_url or "").strip() or None
        self.anthropic_model = (self.anthropic_model or "").strip() or None
        if not self.anthropic_api_key and not self.anthropic_auth_token:
            raise ValueError("set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN")
        return self


class GroupProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    ignore_keywords: list[str] = Field(default_factory=list)


class GroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    profile: str | None = None
    chat_id: int | None = None
    username: str | None = None
    priority: Literal["high", "normal", "low"] = "normal"
    topics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    ignore_keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_selector(self) -> GroupConfig:
        if (self.chat_id is None) == (self.username is None):
            raise ValueError("each group needs exactly one of chat_id or username")
        if self.username:
            self.username = self.username.removeprefix("@").strip()
        return self

    @property
    def selector(self) -> int | str:
        return self.chat_id if self.chat_id is not None else self.username or ""


class SummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron: str = "5 */3 * * *"
    model: str = Field(default="claude-opus-5", min_length=1)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    direct_summary_max_chars: int = Field(default=80_000, ge=1_000)
    chunk_max_chars: int = Field(default=120_000, ge=1_000)
    max_p2_items: int = Field(default=5, ge=0, le=50)
    max_messages_per_run: int = Field(default=5_000, ge=1, le=100_000)
    max_batches_per_trigger: int = Field(default=3, ge=1, le=20)
    output_repair_attempts: int = Field(default=1, ge=0, le=3)
    transient_retry_attempts: int = Field(default=3, ge=0, le=10)
    max_run_failures: int = Field(default=3, ge=1, le=20)


class MaintenanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    retention_days: int = Field(default=90, ge=1, le=3_650)
    cron: str = "30 3 * * 0"
    vacuum: bool = True


class FileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = "Asia/Shanghai"
    initial_backfill_messages: int = Field(default=200, ge=1, le=10_000)
    profiles: dict[str, GroupProfileConfig] = Field(default_factory=dict)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    groups: list[GroupConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_and_resolve(self) -> FileConfig:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc

        resolved: list[GroupConfig] = []
        for group in self.groups:
            if group.profile is None:
                resolved.append(group)
                continue
            profile = self.profiles.get(group.profile)
            if profile is None:
                raise ValueError(
                    f"group {group.name!r} references unknown profile {group.profile!r}"
                )
            resolved.append(
                group.model_copy(
                    update={
                        "topics": _merge_unique(profile.topics, group.topics),
                        "keywords": _merge_unique(profile.keywords, group.keywords),
                        "ignore_keywords": _merge_unique(
                            profile.ignore_keywords, group.ignore_keywords
                        ),
                    }
                )
            )
        self.groups = resolved
        return self


class AppSettings(BaseModel):
    env: EnvironmentSettings
    config: FileConfig

    @property
    def anthropic_model(self) -> str:
        return self.env.anthropic_model or self.config.summary.model

    @property
    def data_dir(self) -> Path:
        return self.env.tg_digest_data_dir

    @property
    def database_path(self) -> Path:
        return self.data_dir / "digest.sqlite3"

    @property
    def log_path(self) -> Path:
        return self.env.tg_digest_log_file or self.data_dir / "tg-digest.log"

    @property
    def user_session_path(self) -> Path:
        return self.data_dir / "user"

    @property
    def bot_session_path(self) -> Path:
        return self.data_dir / "bot"


def ensure_data_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def load_settings(config_path: Path | None = None) -> AppSettings:
    env = EnvironmentSettings()  # type: ignore[call-arg]
    path = config_path or Path(os.environ.get("TG_DIGEST_CONFIG", env.tg_digest_config))
    if not path.exists():
        raise FileNotFoundError(
            f"configuration file not found: {path}; copy config/groups.example.yaml first"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = FileConfig.model_validate(raw)
    ensure_data_dir(env.tg_digest_data_dir)
    return AppSettings(env=env, config=config)
