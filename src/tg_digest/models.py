from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class SummaryCategory(StrEnum):
    MUST_KNOW = "must_know"
    DECISION = "decision"
    ACTION = "action"
    EVENT = "event"
    RESOURCE = "resource"
    OPEN_QUESTION = "open_question"


class SourceRef(StrictModel):
    chat_id: int
    message_id: int


class SummaryItem(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=1200)
    priority: Priority
    category: SummaryCategory
    sources: list[SourceRef] = Field(min_length=1, max_length=10)
    action_required: bool = False
    due_at: str | None = None
    unconfirmed: bool = False


class DigestResult(StrictModel):
    overview: str = Field(default="", max_length=800)
    items: list[SummaryItem] = Field(default_factory=list)
    omitted_count: int = Field(default=0, ge=0)


class NormalizedMessage(StrictModel):
    chat_id: int
    message_id: int
    chat_title: str
    chat_username: str | None = None
    sender_name: str | None = None
    sent_at: datetime
    text: str = ""
    reply_to_message_id: int | None = None
    forwarded_from: str | None = None
    media_type: str | None = None
    urls: list[str] = Field(default_factory=list)
    permalink: str | None = None
    is_admin: bool = False

    @property
    def source_key(self) -> tuple[int, int]:
        return (self.chat_id, self.message_id)


class PreparedChunk(StrictModel):
    records: str
    sources: list[SourceRef]


class PreparedChat(StrictModel):
    chat_id: int
    chat_title: str
    priority: str
    topics: list[str]
    keywords: list[str]
    original_count: int
    kept_count: int
    chunks: list[PreparedChunk]


class PreparedBatch(StrictModel):
    messages: list[NormalizedMessage]
    chats: list[PreparedChat]
    original_count: int
    filtered_count: int
    timezone: str = "UTC"


class SummaryRun(StrictModel):
    id: str
    trigger: Literal["scheduled", "manual", "cli"]
    status: Literal[
        "prepared",
        "generated",
        "delivering",
        "delivered",
        "failed",
        "quarantined",
    ]
    created_at: datetime
    window_start: datetime | None = None
    window_end: datetime | None = None
    digest: DigestResult | None = None
    rendered_parts: list[str] = Field(default_factory=list)
    telegram_message_ids: list[int] = Field(default_factory=list)
    error: str | None = None
    failure_count: int = Field(default=0, ge=0)
