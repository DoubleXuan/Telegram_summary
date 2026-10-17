from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .db import Database, MaintenanceResult
from .delivery import TelegramDelivery
from .preprocess import prepare_messages
from .renderer import render_digest, telegram_parts_fit
from .settings import AppSettings, GroupConfig
from .summarizer import AnthropicSummarizer

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ServiceResult:
    status: str
    run_id: str | None = None
    message_count: int = 0


class DigestService:
    def __init__(
        self,
        settings: AppSettings,
        database: Database,
        summarizer: AnthropicSummarizer,
        delivery: TelegramDelivery,
        group_configs: dict[int, GroupConfig],
    ) -> None:
        self.settings = settings
        self.database = database
        self.summarizer = summarizer
        self.delivery = delivery
        self.group_configs = group_configs
        self._lock = asyncio.Lock()

    async def run_digest(self, trigger: str) -> ServiceResult:
        if self._lock.locked():
            return ServiceResult(status="busy")

        async with self._lock:
            return await self._run_digest_locked(trigger)

    async def drain_scheduled(self) -> ServiceResult:
        async with self._lock:
            total_messages = 0
            last_run_id: str | None = None
            completed_batches = 0
            max_batches = self.settings.config.summary.max_batches_per_trigger

            for _ in range(max_batches):
                result = await self._run_digest_locked("scheduled")
                if result.status == "no_messages":
                    break
                if result.status != "delivered":
                    return result
                completed_batches += 1
                total_messages += result.message_count
                last_run_id = result.run_id

            if completed_batches == 0:
                return ServiceResult(status="no_messages")

            LOGGER.info(
                "Scheduled drain completed %s batch(es) with %s source messages",
                completed_batches,
                total_messages,
            )
            return ServiceResult(
                status="delivered",
                run_id=last_run_id,
                message_count=total_messages,
            )

    async def retry_quarantined(self) -> ServiceResult:
        if self._lock.locked():
            return ServiceResult(status="busy")

        async with self._lock:
            run = await self.database.reactivate_oldest_quarantined()
            if run is None:
                return ServiceResult(status="no_quarantined")
            LOGGER.info("Reactivated quarantined digest %s for manual retry", run.id)
            return await self._run_digest_locked("manual")

    async def run_maintenance(self) -> MaintenanceResult | None:
        if self._lock.locked():
            LOGGER.info("Skipping database maintenance while digest work is active")
            return None

        async with self._lock:
            config = self.settings.config.maintenance
            result = await self.database.run_maintenance(
                config.retention_days,
                vacuum=config.vacuum,
            )
            LOGGER.info(
                "Database maintenance deleted %s run(s) and %s message(s); vacuumed=%s",
                result.runs_deleted,
                result.messages_deleted,
                result.vacuumed,
            )
            return result

    async def _run_digest_locked(self, trigger: str) -> ServiceResult:
        run = await self.database.get_resumable_run()
        if run is None:
            run = await self.database.create_run(
                trigger, self.settings.config.summary.max_messages_per_run
            )
        if run is None:
            return ServiceResult(status="no_messages")

        try:
            messages = await self.database.get_run_messages(run.id)
            digest = run.digest
            if digest is None:
                prepared = prepare_messages(
                    messages,
                    self.group_configs,
                    self.settings.config.summary.chunk_max_chars,
                    self.settings.config.timezone,
                )
                for chat in prepared.chats:
                    LOGGER.info(
                        "Digest preparation: run=%s chat_id=%s original=%s kept=%s "
                        "filtered=%s chunks=%s",
                        run.id,
                        chat.chat_id,
                        chat.original_count,
                        chat.kept_count,
                        chat.original_count - chat.kept_count,
                        len(chat.chunks),
                    )
                digest = await self.summarizer.summarize(prepared, run_id=run.id)
                digest = digest.model_copy(
                    update={
                        "omitted_count": digest.omitted_count + prepared.filtered_count,
                    }
                )

            if not run.rendered_parts or not telegram_parts_fit(run.rendered_parts):
                if run.window_start is None or run.window_end is None:
                    raise ValueError("summary run has no message time window")
                rendered = render_digest(
                    digest,
                    messages,
                    run.id,
                    run.window_start,
                    run.window_end,
                    self.settings.config.timezone,
                )
                reset_delivery = bool(run.telegram_message_ids)
                if reset_delivery:
                    LOGGER.warning(
                        "Re-rendering digest %s after partial delivery; "
                        "recorded part IDs will be reset to avoid omitting changed content",
                        run.id,
                    )
                await self.database.save_generated(
                    run.id,
                    digest,
                    rendered,
                    reset_delivery=reset_delivery,
                )
                run = await self.database.get_run(run.id)
                if run is None:
                    raise RuntimeError("summary run disappeared after generation")

            await self.database.mark_delivering(run.id)
            await self.delivery.send_parts(run.id, run.rendered_parts, run.telegram_message_ids)
            await self.database.complete_run(run.id)
            LOGGER.info("Completed digest %s with %s source messages", run.id, len(messages))
            return ServiceResult(status="delivered", run_id=run.id, message_count=len(messages))
        except Exception as exc:
            failure = await self.database.fail_run(
                run.id,
                type(exc).__name__,
                self.settings.config.summary.max_run_failures,
            )
            if failure.quarantined:
                LOGGER.error(
                    "Digest run %s quarantined after %s failures; "
                    "its messages remain unsummarized and newer runs may continue",
                    run.id,
                    failure.failure_count,
                )
            LOGGER.exception(
                "Digest run %s failed (attempt %s/%s)",
                run.id,
                failure.failure_count,
                self.settings.config.summary.max_run_failures,
            )
            raise
