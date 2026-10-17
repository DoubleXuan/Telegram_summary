import asyncio
from datetime import UTC, datetime

import pytest

from tg_digest.db import Database
from tg_digest.models import (
    DigestResult,
    NormalizedMessage,
    Priority,
    SourceRef,
    SummaryCategory,
    SummaryItem,
)
from tg_digest.service import DigestService
from tg_digest.settings import (
    AppSettings,
    EnvironmentSettings,
    FileConfig,
    GroupConfig,
)


class FakeSummarizer:
    def __init__(self, omitted_count: int = 0) -> None:
        self.omitted_count = omitted_count
        self.calls = 0

    async def summarize(self, batch, *, run_id="-"):  # noqa: ANN001
        self.calls += 1
        source = batch.messages[0]
        return DigestResult(
            omitted_count=self.omitted_count,
            items=[
                SummaryItem(
                    title="Important",
                    summary="Act now",
                    priority=Priority.P1,
                    category=SummaryCategory.ACTION,
                    sources=[SourceRef(chat_id=source.chat_id, message_id=source.message_id)],
                )
            ],
        )


@pytest.mark.asyncio
async def test_delivery_retry_reuses_saved_digest_without_regeneration(tmp_path, caplog):
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    summarizer = FakeSummarizer()
    delivery = FakeDelivery(fail_on_call=1)
    caplog.set_level("INFO")
    try:
        await save_messages(database, 2)
        service = build_service(
            tmp_path,
            database,
            delivery,
            messages_per_run=10,
            batches_per_trigger=3,
            summarizer=summarizer,
        )
        with pytest.raises(RuntimeError, match="delivery failed"):
            await service.run_digest("manual")
        run = await database.get_resumable_run()
        assert run is not None and run.digest is not None
        original = run.digest
        original_parts = run.rendered_parts
        result = await service.run_digest("manual")
        assert result.run_id == run.id
        assert summarizer.calls == 1
        saved = await database.get_run(run.id)
        assert saved is not None
        assert saved.digest == original
        assert saved.rendered_parts == original_parts
        assert await database.unsummarized_count() == 0
        assert "original=2 kept=2 filtered=0 chunks=1" in caplog.text
        assert "Release update" not in caplog.text
    finally:
        await database.close()


class AlwaysFailSummarizer:
    async def summarize(self, batch, *, run_id="-"):  # noqa: ANN001
        raise ValueError("malformed model output")


class FakeDelivery:
    def __init__(self, fail_on_call: int | None = None) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call

    async def send_parts(self, run_id, parts, recorded_ids):  # noqa: ANN001
        self.calls += 1
        assert run_id
        assert parts
        if self.calls == self.fail_on_call:
            raise RuntimeError("delivery failed")
        return [1]


@pytest.mark.asyncio
async def test_service_delivers_once(tmp_path) -> None:
    environment = EnvironmentSettings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_phone="+10000000000",
        telegram_bot_token="token",
        telegram_owner_id=1,
        anthropic_api_key="key",
        tg_digest_data_dir=tmp_path,
    )
    group = GroupConfig(name="Test", chat_id=-1001)
    settings = AppSettings(env=environment, config=FileConfig(groups=[group], timezone="UTC"))
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    delivery = FakeDelivery()
    try:
        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=1,
                chat_title="Test",
                sent_at=datetime.now(UTC),
                text="Release is Friday",
            )
        )
        service = DigestService(
            settings,
            database,
            FakeSummarizer(),  # type: ignore[arg-type]
            delivery,  # type: ignore[arg-type]
            {-1001: group},
        )
        first = await service.run_digest("manual")
        second = await service.run_digest("manual")
        assert first.status == "delivered"
        assert second.status == "no_messages"
        assert delivery.calls == 1
    finally:
        await database.close()


async def save_messages(database: Database, count: int) -> None:
    for message_id in range(1, count + 1):
        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=message_id,
                chat_title="Test",
                sent_at=datetime(2026, 8, 24, 9, message_id, tzinfo=UTC),
                text=f"Release update {message_id}",
            )
        )


def build_service(
    tmp_path,
    database: Database,
    delivery: FakeDelivery,
    *,
    messages_per_run: int,
    batches_per_trigger: int,
    summarizer: FakeSummarizer | AlwaysFailSummarizer | None = None,
    max_run_failures: int = 3,
) -> DigestService:
    environment = EnvironmentSettings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_phone="+10000000000",
        telegram_bot_token="token",
        telegram_owner_id=1,
        anthropic_api_key="key",
        tg_digest_data_dir=tmp_path,
    )
    group = GroupConfig(name="Test", chat_id=-1001)
    config = FileConfig.model_validate(
        {
            "timezone": "UTC",
            "summary": {
                "max_messages_per_run": messages_per_run,
                "max_batches_per_trigger": batches_per_trigger,
                "max_run_failures": max_run_failures,
            },
            "groups": [group.model_dump()],
        }
    )
    return DigestService(
        AppSettings(env=environment, config=config),
        database,
        summarizer or FakeSummarizer(),  # type: ignore[arg-type]
        delivery,  # type: ignore[arg-type]
        {-1001: group},
    )


@pytest.mark.asyncio
async def test_scheduled_drain_processes_multiple_batches(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    delivery = FakeDelivery()
    try:
        await save_messages(database, 5)
        service = build_service(
            tmp_path,
            database,
            delivery,
            messages_per_run=2,
            batches_per_trigger=3,
        )

        result = await service.drain_scheduled()

        assert result.status == "delivered"
        assert result.message_count == 5
        assert delivery.calls == 3
        assert await database.unsummarized_count() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_scheduled_drain_stops_at_batch_limit(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    delivery = FakeDelivery()
    try:
        await save_messages(database, 7)
        service = build_service(
            tmp_path,
            database,
            delivery,
            messages_per_run=2,
            batches_per_trigger=3,
        )

        result = await service.drain_scheduled()

        assert result.status == "delivered"
        assert result.message_count == 6
        assert delivery.calls == 3
        assert await database.unsummarized_count() == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_scheduled_drain_preserves_failed_second_batch(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    delivery = FakeDelivery(fail_on_call=2)
    try:
        await save_messages(database, 4)
        service = build_service(
            tmp_path,
            database,
            delivery,
            messages_per_run=2,
            batches_per_trigger=3,
        )

        with pytest.raises(RuntimeError, match="delivery failed"):
            await service.drain_scheduled()

        resumable = await database.get_resumable_run()
        assert resumable is not None
        assert resumable.status == "failed"
        assert delivery.calls == 2
        assert await database.unsummarized_count() == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_scheduled_drain_waits_for_other_service_work(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    try:
        service = build_service(
            tmp_path,
            database,
            FakeDelivery(),
            messages_per_run=2,
            batches_per_trigger=3,
        )
        await service._lock.acquire()
        task = asyncio.create_task(service.drain_scheduled())
        await asyncio.sleep(0)
        assert task.done() is False

        service._lock.release()
        result = await task

        assert result.status == "no_messages"
    finally:
        if service._lock.locked():
            service._lock.release()
        await database.close()


@pytest.mark.asyncio
async def test_maintenance_returns_none_during_digest_work(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    try:
        service = build_service(
            tmp_path,
            database,
            FakeDelivery(),
            messages_per_run=2,
            batches_per_trigger=3,
        )
        await service._lock.acquire()
        try:
            result = await service.run_maintenance()
        finally:
            service._lock.release()

        assert result is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_service_combines_model_and_local_omitted_counts(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    try:
        for message_id, text in enumerate(
            ["Important release", "Another relevant update", "收到"],
            start=1,
        ):
            await database.save_message(
                NormalizedMessage(
                    chat_id=-1001,
                    message_id=message_id,
                    chat_title="Test",
                    sent_at=datetime(2026, 8, 24, 9, message_id, tzinfo=UTC),
                    text=text,
                )
            )
        service = build_service(
            tmp_path,
            database,
            FakeDelivery(),
            messages_per_run=10,
            batches_per_trigger=3,
            summarizer=FakeSummarizer(omitted_count=1),
        )

        result = await service.run_digest("manual")

        assert result.run_id is not None
        stored = await database.get_run(result.run_id)
        assert stored is not None
        assert stored.digest is not None
        assert stored.digest.omitted_count == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_quarantined_run_does_not_block_newer_messages(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    delivery = FakeDelivery()
    try:
        await save_messages(database, 1)
        service = build_service(
            tmp_path,
            database,
            delivery,
            messages_per_run=1,
            batches_per_trigger=3,
            summarizer=AlwaysFailSummarizer(),
            max_run_failures=2,
        )

        for _ in range(2):
            with pytest.raises(ValueError, match="malformed model output"):
                await service.run_digest("manual")

        assert await database.quarantined_count() == 1
        assert await database.get_resumable_run() is None

        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=2,
                chat_title="Test",
                sent_at=datetime(2026, 8, 24, 10, tzinfo=UTC),
                text="Newer release update",
            )
        )
        service.summarizer = FakeSummarizer()  # type: ignore[assignment]
        result = await service.run_digest("manual")

        assert result.status == "delivered"
        assert result.message_count == 1
        assert await database.unsummarized_count() == 1
        assert delivery.calls == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_retry_quarantined_reactivates_original_snapshot(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    delivery = FakeDelivery()
    try:
        await save_messages(database, 1)
        service = build_service(
            tmp_path,
            database,
            delivery,
            messages_per_run=1,
            batches_per_trigger=3,
            summarizer=AlwaysFailSummarizer(),
            max_run_failures=1,
        )
        with pytest.raises(ValueError):
            await service.run_digest("manual")

        service.summarizer = FakeSummarizer()  # type: ignore[assignment]
        result = await service.retry_quarantined()

        assert result.status == "delivered"
        assert await database.quarantined_count() == 0
        assert await database.unsummarized_count() == 0
        assert delivery.calls == 1
    finally:
        await database.close()
