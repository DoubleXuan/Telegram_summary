from __future__ import annotations

import logging

from telethon import events

from .db import Database
from .delivery import TelegramDelivery
from .ingestion import IngestionService
from .scheduler import create_scheduler
from .service import DigestService, ServiceResult
from .settings import AppSettings
from .summarizer import AnthropicSummarizer
from .telegram_clients import TelegramClients

LOGGER = logging.getLogger(__name__)


async def run_scheduled_digest(
    ingestion: IngestionService,
    service: DigestService,
) -> ServiceResult:
    caught_up = await ingestion.catch_up_now()
    LOGGER.info("Scheduled catch-up saved %s new messages", caught_up)
    result = await service.drain_scheduled()
    LOGGER.info(
        "Scheduled digest finished: status=%s messages=%s",
        result.status,
        result.message_count,
    )
    return result


async def run_application(settings: AppSettings, run_once: bool = False) -> None:
    database = Database(settings.database_path)
    clients = TelegramClients(settings)
    ingestion: IngestionService | None = None
    scheduler = None

    await database.connect()
    try:
        await clients.start()
        ingestion = IngestionService(
            clients.user,
            database,
            settings.config.groups,
            initial_backfill_messages=settings.config.initial_backfill_messages,
        )
        await ingestion.start()

        summarizer = AnthropicSummarizer(
            settings.config.summary,
            api_key=settings.env.anthropic_api_key,
            auth_token=settings.env.anthropic_auth_token,
            base_url=settings.env.anthropic_base_url,
            model=settings.anthropic_model,
            compatibility_mode=settings.env.anthropic_compatibility_mode,
        )
        delivery = TelegramDelivery(clients.bot, settings.env.telegram_owner_id, database)
        service = DigestService(
            settings,
            database,
            summarizer,
            delivery,
            ingestion.group_configs,
        )

        if run_once:
            result = await service.run_digest("cli")
            print(f"summary result: {result.status}")
            return

        async def command_handler(event: events.NewMessage.Event) -> None:
            command = (event.raw_text or "").split()[0].split("@")[0].lower()
            if command == "/summary":
                await event.respond("正在生成摘要，请稍候……")
                try:
                    caught_up = await ingestion.catch_up_now()
                    LOGGER.info("Manual catch-up saved %s new messages", caught_up)
                    result = await service.run_digest("manual")
                    if result.status == "no_messages":
                        await event.respond("目前没有尚未总结的新消息。")
                    elif result.status == "busy":
                        await event.respond("已有摘要任务正在运行。")
                except Exception:
                    await event.respond("摘要生成失败，运行状态已保留，将在下次重试。")
            elif command == "/retry-failed":
                await event.respond("正在重试最早的隔离摘要，请稍候……")
                try:
                    result = await service.retry_quarantined()
                    if result.status == "no_quarantined":
                        await event.respond("目前没有需要人工重试的隔离摘要。")
                    elif result.status == "busy":
                        await event.respond("已有摘要任务正在运行。")
                except Exception:
                    await event.respond("隔离摘要重试失败，消息仍已安全保留。")
            elif command == "/status":
                count = await database.unsummarized_count()
                quarantined = await database.quarantined_count()
                await event.respond(
                    f"当前有 {count} 条尚未总结的消息，其中有 {quarantined} 个隔离摘要运行。"
                )
            else:
                await event.respond(
                    "可用命令：/summary 立即总结，/status 查看待处理数量，"
                    "/retry-failed 重试隔离摘要。"
                )

        clients.bot.add_event_handler(
            command_handler,
            events.NewMessage(
                from_users=settings.env.telegram_owner_id,
                pattern=r"^/(start|help|summary|status|retry-failed)(?:@\w+)?(?:\s|$)",
            ),
        )

        pending = await database.get_resumable_run()
        if pending is not None:
            try:
                await service.run_digest("scheduled")
            except Exception:
                LOGGER.exception("Could not recover pending digest during startup")

        maintenance = settings.config.maintenance
        scheduler = create_scheduler(
            settings.config.summary.cron,
            settings.config.timezone,
            lambda: run_scheduled_digest(ingestion, service),
            maintenance_cron=maintenance.cron if maintenance.enabled else None,
            maintenance_callback=service.run_maintenance if maintenance.enabled else None,
        )
        scheduler.start()
        LOGGER.info("Telegram digest service started")
        await clients.bot.run_until_disconnected()
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=True)
        if ingestion is not None:
            await ingestion.stop()
        await clients.disconnect()
        await database.close()
