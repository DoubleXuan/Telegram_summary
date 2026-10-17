from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .settings import AppSettings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Telethon logs every reconnect and update difference at INFO, and httpx logs every request;
# together they drown out application logs and shorten the rotation window.
NOISY_LOGGERS = ("telethon", "httpx")


def configure_logging(settings: AppSettings) -> None:
    level = getattr(logging, settings.env.tg_digest_log_level.upper(), logging.INFO)
    root = logging.getLogger()

    for handler in list(root.handlers):
        if getattr(handler, "_tg_digest_handler", False):
            root.removeHandler(handler)
            handler.close()

    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = RotatingFileHandler(
        settings.log_path,
        maxBytes=settings.env.tg_digest_log_max_bytes,
        backupCount=settings.env.tg_digest_log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler._tg_digest_handler = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    if settings.env.tg_digest_log_console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler._tg_digest_handler = True  # type: ignore[attr-defined]
        root.addHandler(stream_handler)

    root.setLevel(level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
