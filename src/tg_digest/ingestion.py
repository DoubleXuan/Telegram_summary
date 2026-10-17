from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC
from typing import Any

from telethon import TelegramClient, events, utils

from .db import Database
from .models import NormalizedMessage
from .settings import GroupConfig

LOGGER = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
SAVE_BATCH_SIZE = 200


def build_permalink(chat_id: int, username: str | None, message_id: int) -> str | None:
    if username:
        return f"https://t.me/{username.removeprefix('@')}/{message_id}"
    chat_id_text = str(chat_id)
    if chat_id_text.startswith("-100"):
        return f"https://t.me/c/{chat_id_text[4:]}/{message_id}"
    return None


def _display_name(entity: Any) -> str | None:
    if entity is None:
        return None
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(str(part) for part in parts if part).strip()
    if name:
        return name
    username = getattr(entity, "username", None)
    return f"@{username}" if username else None


def _forwarded_from(message: Any) -> str | None:
    forwarded = getattr(message, "fwd_from", None)
    if forwarded is None:
        return None
    if getattr(forwarded, "from_name", None):
        return str(forwarded.from_name)
    from_id = getattr(forwarded, "from_id", None)
    return str(from_id) if from_id is not None else "forwarded"


def _extract_urls(message: Any) -> list[str]:
    text = getattr(message, "raw_text", "") or ""
    urls = set(URL_RE.findall(text))
    for entity in getattr(message, "entities", None) or []:
        explicit = getattr(entity, "url", None)
        if explicit:
            urls.add(str(explicit))
    return sorted(urls)


async def normalize_message(
    client: TelegramClient, message: Any, chat: Any | None = None
) -> NormalizedMessage:
    chat = chat or await message.get_chat()
    sender = await message.get_sender()
    chat_id = int(utils.get_peer_id(chat))
    username = getattr(chat, "username", None)
    sent_at = message.date
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    return NormalizedMessage(
        chat_id=chat_id,
        message_id=int(message.id),
        chat_title=_display_name(chat) or str(chat_id),
        chat_username=username,
        sender_name=_display_name(sender),
        sent_at=sent_at,
        text=getattr(message, "raw_text", "") or "",
        reply_to_message_id=getattr(message, "reply_to_msg_id", None),
        forwarded_from=_forwarded_from(message),
        media_type=type(message.media).__name__ if getattr(message, "media", None) else None,
        urls=_extract_urls(message),
        permalink=build_permalink(chat_id, username, int(message.id)),
        is_admin=bool(getattr(message, "post", False)),
    )


class IngestionService:
    def __init__(
        self,
        client: TelegramClient,
        database: Database,
        groups: list[GroupConfig],
        initial_backfill_messages: int = 200,
    ) -> None:
        self.client = client
        self.database = database
        self.groups = groups
        self.initial_backfill_messages = initial_backfill_messages
        self.entities: dict[int, Any] = {}
        self.group_configs: dict[int, GroupConfig] = {}
        self._handler: Callable[..., Awaitable[None]] | None = None

    async def start(self) -> None:
        await self._resolve_groups()
        catch_up_cursors = await self._cursor_snapshot()

        async def handle(event: events.NewMessage.Event) -> None:
            entity = self.entities.get(event.chat_id)
            if entity is None:
                return
            try:
                # Reuse the resolved entity: fetching the chat per event triggers
                # GetChannelsRequest floods. Live events can skip IDs when a save fails, so
                # they never advance the cursor; scheduled catch-up fills gaps in ID order.
                normalized = await normalize_message(self.client, event.message, chat=entity)
                await self.database.save_message(normalized, advance_cursor=False)
            except Exception:
                LOGGER.exception("Failed to persist Telegram event")

        self._handler = handle
        self.client.add_event_handler(handle, events.NewMessage(chats=list(self.entities.values())))
        await self.catch_up(catch_up_cursors)

    async def stop(self) -> None:
        if self._handler is not None:
            self.client.remove_event_handler(self._handler)
            self._handler = None

    async def _resolve_groups(self) -> None:
        for group in self.groups:
            entity = await self.client.get_entity(group.selector)
            chat_id = int(utils.get_peer_id(entity))
            self.entities[chat_id] = entity
            self.group_configs[chat_id] = group
            if group.chat_id is not None and group.chat_id != chat_id:
                raise ValueError(
                    f"configured chat_id {group.chat_id} for {group.name!r} resolved to {chat_id}"
                )
            LOGGER.info("Monitoring Telegram chat %s (%s)", group.name, chat_id)

    async def _cursor_snapshot(self) -> dict[int, int]:
        return {chat_id: await self.database.get_cursor(chat_id) for chat_id in self.entities}

    async def catch_up_now(self) -> int:
        return await self.catch_up(await self._cursor_snapshot())

    async def catch_up(self, cursors: dict[int, int] | None = None) -> int:
        total_count = 0
        for chat_id, entity in self.entities.items():
            cursor = (
                cursors[chat_id] if cursors is not None else await self.database.get_cursor(chat_id)
            )
            if cursor:
                messages = self.client.iter_messages(entity, min_id=cursor, reverse=True)
            else:
                recent = [
                    message
                    async for message in self.client.iter_messages(
                        entity, limit=self.initial_backfill_messages
                    )
                ]
                LOGGER.info(
                    "Initializing chat %s from its %s most recent messages",
                    chat_id,
                    self.initial_backfill_messages,
                )
                messages = _iterate(reversed(recent))
            count = 0
            batch: list[NormalizedMessage] = []
            async for message in messages:
                batch.append(await normalize_message(self.client, message, chat=entity))
                if len(batch) >= SAVE_BATCH_SIZE:
                    count += await self.database.save_messages(batch)
                    batch = []
            count += await self.database.save_messages(batch)
            total_count += count
            LOGGER.info("Caught up chat %s with %s new messages", chat_id, count)
        return total_count


async def _iterate(items: Iterable[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item
