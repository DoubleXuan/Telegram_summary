from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import utils
from telethon.extensions import html as telethon_html

from .models import DigestResult, NormalizedMessage, SummaryCategory, SummaryItem

SECTION_NAMES = {
    SummaryCategory.MUST_KNOW: "🔴 需要关注",
    SummaryCategory.DECISION: "✅ 明确结论",
    SummaryCategory.ACTION: "📌 待办事项",
    SummaryCategory.EVENT: "🗓 时间与活动",
    SummaryCategory.RESOURCE: "🔗 值得查看",
    SummaryCategory.OPEN_QUESTION: "💬 热门未决问题",
}


def _source_html(
    item: SummaryItem,
    message_map: dict[tuple[int, int], NormalizedMessage],
    zone: ZoneInfo,
) -> str:
    rendered: list[str] = []
    for source in item.sources:
        message = message_map.get((source.chat_id, source.message_id))
        if not message:
            continue
        local_sent_at = message.sent_at.astimezone(zone)
        label = f"{message.chat_title} · {local_sent_at:%H:%M}"
        if message.permalink:
            rendered.append(
                f'<a href="{html.escape(message.permalink, quote=True)}">{html.escape(label)}</a>'
            )
        else:
            rendered.append(html.escape(f"{label} · #{message.message_id}"))
    return "；".join(rendered)


def _item_block(
    item: SummaryItem,
    message_map: dict[tuple[int, int], NormalizedMessage],
    zone: ZoneInfo,
) -> str:
    flags: list[str] = [f"<b>{html.escape(item.priority.value)}</b>"]
    if item.unconfirmed:
        flags.append("尚未确认")
    if item.due_at:
        flags.append(f"截止/时间：{html.escape(item.due_at)}")
    source = _source_html(item, message_map, zone)
    return (
        f"• {' · '.join(flags)} <b>{html.escape(item.title)}</b>\n"
        f"  {html.escape(item.summary)}" + (f"\n  来源：{source}" if source else "")
    )


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _split_html(value: str, max_units: int) -> list[str]:
    text, entities = telethon_html.parse(value)
    return [
        telethon_html.unparse(part_text, part_entities)
        for part_text, part_entities in utils.split_text(text, entities, limit=max_units)
    ]


def telegram_parts_fit(parts: list[str], max_units: int = 4_096) -> bool:
    for part in parts:
        text, _ = telethon_html.parse(part)
        if _utf16_units(text) > max_units:
            return False
    return True


def _split_blocks(blocks: list[str], max_chars: int = 3_600) -> list[str]:
    parts: list[str] = []
    current = ""
    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if current and len(candidate) > max_chars:
            parts.extend(_split_html(current, max_chars))
            current = block
        else:
            current = candidate
    if current:
        parts.extend(_split_html(current, max_chars))
    return parts


def render_digest(
    digest: DigestResult,
    messages: list[NormalizedMessage],
    run_id: str,
    window_start: datetime,
    window_end: datetime,
    timezone: str,
) -> list[str]:
    zone = ZoneInfo(timezone)
    local_start = window_start.astimezone(zone)
    local_end = window_end.astimezone(zone)
    group_count = len({message.chat_id for message in messages})
    blocks = [
        "📬 <b>Telegram 群消息摘要</b>\n"
        f"时间：{local_start:%m-%d %H:%M}–{local_end:%m-%d %H:%M}\n"
        f"覆盖：{group_count} 个群，共 {len(messages)} 条消息"
    ]
    if digest.overview:
        blocks.append(html.escape(digest.overview))

    grouped: dict[SummaryCategory, list[SummaryItem]] = defaultdict(list)
    for item in digest.items:
        grouped[item.category].append(item)
    message_map = {message.source_key: message for message in messages}
    for category, heading in SECTION_NAMES.items():
        items = grouped.get(category)
        if not items:
            continue
        blocks.append(
            f"<b>{heading}</b>\n"
            + "\n\n".join(_item_block(item, message_map, zone) for item in items)
        )

    if not digest.items:
        blocks.append("本周期没有识别到需要关注的关键信息。")
    if digest.omitted_count:
        blocks.append(f"其余约 {digest.omitted_count} 条主要为闲聊、重复或低优先级信息，已省略。")

    parts = _split_blocks(blocks)
    total = len(parts)
    return [
        f"{part}\n\n<code>摘要编号：{run_id}:{index}/{total}</code>"
        for index, part in enumerate(parts, start=1)
    ]
