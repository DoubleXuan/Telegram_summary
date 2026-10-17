from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

from .models import NormalizedMessage, PreparedBatch, PreparedChat, PreparedChunk, SourceRef
from .settings import GroupConfig

SPACE_RE = re.compile(r"\s+")
LOW_VALUE_EXACT = {
    "早",
    "早安",
    "晚安",
    "签到",
    "打卡",
    "收到",
    "好的",
    "谢谢",
    "感谢",
    "+1",
    "ok",
    "thanks",
    "thank you",
}


def _normalized_text(text: str) -> str:
    return SPACE_RE.sub(" ", text).strip().casefold()


def _has_substance(text: str) -> bool:
    return any(character.isalnum() for character in text)


def _should_keep(message: NormalizedMessage, group: GroupConfig | None) -> bool:
    text = _normalized_text(message.text)
    if not text:
        return False
    if text in LOW_VALUE_EXACT:
        return False
    if not _has_substance(text) and not message.urls:
        return False
    if group:
        positive = any(keyword.casefold() in text for keyword in group.keywords)
        ignored = any(keyword.casefold() in text for keyword in group.ignore_keywords)
        if ignored and not positive:
            return False
    return True


def _record(message: NormalizedMessage, zone: ZoneInfo) -> str:
    # Chat-level context (title, priority, topics, keywords) is sent once per chunk in the
    # prompt, so each record only carries per-message fields and omits empty values.
    payload: dict[str, object] = {
        "source": {"chat_id": message.chat_id, "message_id": message.message_id},
        "sent_at": message.sent_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
    }
    if message.sender_name:
        payload["sender"] = message.sender_name
    if message.reply_to_message_id:
        payload["reply_to_message_id"] = message.reply_to_message_id
    if message.forwarded_from:
        payload["forwarded_from"] = message.forwarded_from
    if message.media_type:
        payload["media_type"] = message.media_type
    hidden_urls = [url for url in message.urls if url not in message.text]
    if hidden_urls:
        payload["urls"] = hidden_urls
    payload["text"] = message.text
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _chunk_records(
    messages: list[NormalizedMessage], zone: ZoneInfo, max_chars: int
) -> list[PreparedChunk]:
    chunks: list[PreparedChunk] = []
    records: list[str] = []
    sources: list[SourceRef] = []
    current_size = 0
    for message in messages:
        record = _record(message, zone)
        additional = len(record) + (1 if records else 0)
        if records and current_size + additional > max_chars:
            chunks.append(PreparedChunk(records="\n".join(records), sources=sources))
            records = []
            sources = []
            current_size = 0
        current_size += len(record) + (1 if records else 0)
        records.append(record)
        sources.append(SourceRef(chat_id=message.chat_id, message_id=message.message_id))
    if records:
        chunks.append(PreparedChunk(records="\n".join(records), sources=sources))
    return chunks


def prepare_messages(
    messages: list[NormalizedMessage],
    group_configs: dict[int, GroupConfig],
    chunk_max_chars: int,
    timezone: str = "UTC",
) -> PreparedBatch:
    zone = ZoneInfo(timezone)
    kept: list[NormalizedMessage] = []
    seen: set[tuple[int, str, tuple[str, ...]]] = set()

    for message in sorted(messages, key=lambda item: (item.sent_at, item.chat_id, item.message_id)):
        group = group_configs.get(message.chat_id)
        if not _should_keep(message, group):
            continue
        fingerprint = (message.chat_id, _normalized_text(message.text), tuple(message.urls))
        if fingerprint in seen and not message.reply_to_message_id:
            continue
        seen.add(fingerprint)
        kept.append(message)

    original_counts = Counter(message.chat_id for message in messages)
    by_chat: dict[int, list[NormalizedMessage]] = defaultdict(list)
    for message in kept:
        by_chat[message.chat_id].append(message)
    chats: list[PreparedChat] = []
    titles = {message.chat_id: message.chat_title for message in messages}
    for chat_id, count in original_counts.items():
        group = group_configs.get(chat_id)
        chat_messages = by_chat[chat_id]
        chats.append(
            PreparedChat(
                chat_id=chat_id,
                chat_title=titles[chat_id],
                priority=group.priority if group else "normal",
                topics=group.topics if group else [],
                keywords=group.keywords if group else [],
                original_count=count,
                kept_count=len(chat_messages),
                chunks=_chunk_records(chat_messages, zone, chunk_max_chars),
            )
        )
    return PreparedBatch(
        messages=kept,
        chats=chats,
        original_count=len(messages),
        filtered_count=len(messages) - len(kept),
        timezone=timezone,
    )
