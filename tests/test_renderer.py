from datetime import UTC, datetime

from telethon.extensions import html as telethon_html

from tg_digest.models import (
    DigestResult,
    NormalizedMessage,
    Priority,
    SourceRef,
    SummaryCategory,
    SummaryItem,
)
from tg_digest.renderer import render_digest, telegram_parts_fit


def test_render_digest_escapes_html_and_links_source() -> None:
    sent_at = datetime(2026, 8, 24, 1, 30, tzinfo=UTC)
    message = NormalizedMessage(
        chat_id=-1001,
        message_id=7,
        chat_title="Dev & Ops",
        sent_at=sent_at,
        text="source",
        permalink="https://t.me/example/7",
    )
    digest = DigestResult(
        overview="A < B",
        items=[
            SummaryItem(
                title="API <v2>",
                summary="Stop using A & migrate.",
                priority=Priority.P0,
                category=SummaryCategory.ACTION,
                sources=[SourceRef(chat_id=-1001, message_id=7)],
            )
        ],
    )
    parts = render_digest(digest, [message], "run123", sent_at, sent_at, "UTC")
    rendered = "\n".join(parts)
    assert "A &lt; B" in rendered
    assert "API &lt;v2&gt;" in rendered
    assert "Dev &amp; Ops · 01:30" in rendered
    assert 'href="https://t.me/example/7"' in rendered
    assert "摘要编号：run123:1/1" in rendered


def test_render_digest_localizes_header_and_source_times() -> None:
    sent_at = datetime(2026, 8, 24, 1, 30, tzinfo=UTC)
    message = NormalizedMessage(
        chat_id=-1001,
        message_id=7,
        chat_title="Dev & Ops",
        sent_at=sent_at,
        text="source",
        permalink="https://t.me/example/7",
    )
    digest = DigestResult(
        items=[
            SummaryItem(
                title="API release",
                summary="Review the changes.",
                priority=Priority.P1,
                category=SummaryCategory.MUST_KNOW,
                sources=[SourceRef(chat_id=-1001, message_id=7)],
            )
        ]
    )

    parts = render_digest(
        digest,
        [message],
        "shanghai-run",
        sent_at,
        sent_at,
        "Asia/Shanghai",
    )
    rendered = "\n".join(parts)

    assert "时间：08-24 09:30–08-24 09:30" in rendered
    assert '<a href="https://t.me/example/7">Dev &amp; Ops · 09:30</a>' in rendered
    assert "Dev &amp; Ops · 01:30" not in rendered


def test_render_digest_splits_oversized_category_for_telegram() -> None:
    sent_at = datetime(2026, 8, 24, 1, 30, tzinfo=UTC)
    message = NormalizedMessage(
        chat_id=-1001,
        message_id=7,
        chat_title="Deals",
        sent_at=sent_at,
        text="source",
        permalink="https://t.me/example/7",
    )
    items = [
        SummaryItem(
            title=f"Offer {index}",
            summary="奖励活动详情与限制条件。" * 45,
            priority=Priority.P1,
            category=SummaryCategory.MUST_KNOW,
            sources=[SourceRef(chat_id=-1001, message_id=7)],
        )
        for index in range(10)
    ]

    parts = render_digest(
        DigestResult(items=items),
        [message],
        "longrun",
        sent_at,
        sent_at,
        "UTC",
    )

    assert len(parts) > 2
    assert telegram_parts_fit(parts)
    for part in parts:
        text, _ = telethon_html.parse(part)
        assert len(text.encode("utf-16-le")) // 2 <= 4_096
