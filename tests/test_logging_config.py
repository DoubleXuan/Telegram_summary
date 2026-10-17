import logging
from logging.handlers import RotatingFileHandler

from tg_digest.logging_config import configure_logging
from tg_digest.settings import AppSettings, EnvironmentSettings, FileConfig, GroupConfig


def test_configure_logging_adds_rotating_handler_without_duplicates(tmp_path) -> None:
    environment = EnvironmentSettings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_phone="+10000000000",
        telegram_bot_token="token",
        telegram_owner_id=1,
        anthropic_api_key="key",
        tg_digest_data_dir=tmp_path,
        tg_digest_log_max_bytes=2048,
        tg_digest_log_backup_count=2,
        tg_digest_log_console=False,
    )
    settings = AppSettings(
        env=environment,
        config=FileConfig(groups=[GroupConfig(name="Test", chat_id=-1001)]),
    )

    configure_logging(settings)
    configure_logging(settings)

    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_tg_digest_handler", False)
    ]
    assert len(handlers) == 1
    assert isinstance(handlers[0], RotatingFileHandler)
    assert handlers[0].maxBytes == 2048
    assert handlers[0].backupCount == 2
    assert settings.log_path == tmp_path / "tg-digest.log"
    assert logging.getLogger("telethon").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING
