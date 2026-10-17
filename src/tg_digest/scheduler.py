from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

LOGGER = logging.getLogger(__name__)


def create_scheduler(
    cron: str,
    timezone: str,
    callback: Callable[[], Awaitable[object]],
    *,
    maintenance_cron: str | None = None,
    maintenance_callback: Callable[[], Awaitable[object]] | None = None,
) -> AsyncIOScheduler:
    zone = ZoneInfo(timezone)
    scheduler = AsyncIOScheduler(timezone=zone)
    trigger = CronTrigger.from_crontab(cron, timezone=zone)

    async def guarded_callback() -> None:
        try:
            await callback()
        except Exception:
            LOGGER.exception("Scheduled digest failed")
            raise

    scheduler.add_job(
        guarded_callback,
        trigger=trigger,
        id="telegram-digest",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=1_800,
    )

    if maintenance_cron is not None and maintenance_callback is not None:
        maintenance_trigger = CronTrigger.from_crontab(maintenance_cron, timezone=zone)

        async def guarded_maintenance() -> None:
            try:
                await maintenance_callback()
            except Exception:
                LOGGER.exception("Scheduled database maintenance failed")
                raise

        scheduler.add_job(
            guarded_maintenance,
            trigger=maintenance_trigger,
            id="telegram-digest-maintenance",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3_600,
        )
    elif maintenance_cron is not None or maintenance_callback is not None:
        raise ValueError("maintenance cron and callback must be configured together")

    return scheduler
