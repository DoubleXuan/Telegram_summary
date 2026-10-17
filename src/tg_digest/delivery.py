from __future__ import annotations

import logging

from telethon import TelegramClient

from .db import Database

LOGGER = logging.getLogger(__name__)


class TelegramDelivery:
    def __init__(self, bot: TelegramClient, owner_id: int, database: Database) -> None:
        self.bot = bot
        self.owner_id = owner_id
        self.database = database

    async def send_parts(self, run_id: str, parts: list[str], recorded_ids: list[int]) -> list[int]:
        # Bots cannot read private chat history, so the database receipts are the only record of
        # which parts were already sent.
        delivered = list(recorded_ids)
        for index, part in enumerate(parts, start=1):
            if index <= len(delivered):
                continue
            message = await self.bot.send_message(
                self.owner_id,
                part,
                parse_mode="html",
                link_preview=False,
            )
            await self.database.record_delivery(run_id, int(message.id))
            delivered.append(int(message.id))
            LOGGER.info("Delivered digest %s part %s/%s", run_id, index, len(parts))
        return delivered
