from pathlib import Path

import pytest
from pydantic import ValidationError

from tg_digest.settings import FileConfig, load_settings


def write_config(tmp_path, model: str = "claude-opus-5") -> Path:
    config = tmp_path / "groups.yaml"
    config.write_text(
        f"""
timezone: UTC
summary:
  model: {model}
groups:
  - name: Test
    chat_id: -100123
""",
        encoding="utf-8",
    )
    return config


def set_telegram_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    values = {
        "TELEGRAM_API_ID": "1",
        "TELEGRAM_API_HASH": "hash",
        "TELEGRAM_PHONE": "+10000000000",
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_OWNER_ID": "123",
        "TG_DIGEST_DATA_DIR": str(tmp_path / "data"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_load_settings_with_api_key(monkeypatch, tmp_path) -> None:
    config = write_config(tmp_path)
    set_telegram_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    settings = load_settings(config)
    assert settings.config.timezone == "UTC"
    assert settings.config.initial_backfill_messages == 200
    assert settings.config.groups[0].selector == -100123
    assert settings.anthropic_model == "claude-opus-5"
    assert settings.database_path == Path(tmp_path / "data" / "digest.sqlite3")
    assert settings.data_dir.stat().st_mode & 0o777 == 0o700


def test_load_settings_with_local_gateway(monkeypatch, tmp_path) -> None:
    config = write_config(tmp_path, model="yaml-model")
    set_telegram_environment(monkeypatch, tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8317")
    monkeypatch.setenv("ANTHROPIC_MODEL", "gpt-5.5")
    monkeypatch.setenv("ANTHROPIC_COMPATIBILITY_MODE", "true")

    settings = load_settings(config)
    assert settings.env.anthropic_api_key is None
    assert settings.env.anthropic_auth_token == "gateway-token"
    assert settings.env.anthropic_base_url == "http://localhost:8317"
    assert settings.env.anthropic_compatibility_mode is True
    assert settings.anthropic_model == "gpt-5.5"
    assert settings.log_path == tmp_path / "data" / "tg-digest.log"


def test_load_settings_requires_model_credentials(monkeypatch, tmp_path) -> None:
    config = write_config(tmp_path)
    set_telegram_environment(monkeypatch, tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN"):
        load_settings(config)


def test_group_profile_merges_additional_values_without_duplicates() -> None:
    config = FileConfig.model_validate(
        {
            "profiles": {
                "shared": {
                    "topics": ["Banking"],
                    "keywords": ["Bonus", "KYC"],
                    "ignore_keywords": ["Advertisement"],
                }
            },
            "groups": [
                {
                    "name": "Deals",
                    "profile": "shared",
                    "chat_id": -1001,
                    "topics": ["VPS"],
                    "keywords": ["bonus", "Refund"],
                }
            ],
        }
    )

    group = config.groups[0]
    assert group.topics == ["Banking", "VPS"]
    assert group.keywords == ["Bonus", "KYC", "Refund"]
    assert group.ignore_keywords == ["Advertisement"]


def test_group_profile_must_exist() -> None:
    with pytest.raises(ValidationError, match="unknown profile"):
        FileConfig.model_validate(
            {
                "groups": [
                    {
                        "name": "Deals",
                        "profile": "missing",
                        "chat_id": -1001,
                    }
                ]
            }
        )
