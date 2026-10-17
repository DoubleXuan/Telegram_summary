import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from tg_digest.db import Database
from tg_digest.models import DigestResult, NormalizedMessage


@pytest.mark.asyncio
async def test_database_deduplicates_and_completes_run(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    try:
        message = NormalizedMessage(
            chat_id=-1001,
            message_id=10,
            chat_title="Test",
            sent_at=datetime(2026, 8, 24, 9, tzinfo=UTC),
            text="Important update",
        )
        assert await database.save_message(message) is True
        assert await database.save_message(message) is False
        assert await database.get_cursor(-1001) == 10
        assert await database.unsummarized_count() == 1

        run = await database.create_run("manual", 100)
        assert run is not None
        assert [item.message_id for item in await database.get_run_messages(run.id)] == [10]
        await database.save_generated(run.id, DigestResult(), ["part"])
        await database.mark_delivering(run.id)
        await database.record_delivery(run.id, 55)
        await database.complete_run(run.id)

        stored = await database.get_run(run.id)
        assert stored is not None
        assert stored.status == "delivered"
        assert stored.telegram_message_ids == [55]
        assert await database.unsummarized_count() == 0
        assert await database.create_run("manual", 100) is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_database_serializes_concurrent_writes(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    try:
        messages = [
            NormalizedMessage(
                chat_id=-1001,
                message_id=message_id,
                chat_title="Test",
                sent_at=datetime.now(UTC),
                text=f"Update {message_id}",
            )
            for message_id in range(1, 51)
        ]
        results = await asyncio.gather(*(database.save_message(message) for message in messages))
        assert all(results)
        assert await database.unsummarized_count() == 50
        assert await database.get_cursor(-1001) == 50
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_run_is_resumable(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    try:
        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=1,
                chat_title="Test",
                sent_at=datetime.now(UTC),
                text="Update",
            )
        )
        run = await database.create_run("scheduled", 100)
        assert run is not None
        await database.fail_run(run.id, "temporary")
        resumed = await database.get_resumable_run()
        assert resumed is not None
        assert resumed.id == run.id
        assert resumed.status == "failed"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_maintenance_deletes_only_expired_delivered_data(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    old = now - timedelta(days=120)
    recent = now - timedelta(days=1)
    try:
        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=1,
                chat_title="Test",
                sent_at=old,
                text="Old delivered",
            )
        )
        old_run = await database.create_run("scheduled", 1)
        assert old_run is not None
        await database.complete_run(old_run.id)
        await database._write(
            "UPDATE summary_runs SET created_at = ? WHERE id = ?",
            (old.isoformat(), old_run.id),
        )

        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=2,
                chat_title="Test",
                sent_at=recent,
                text="Recent delivered",
            )
        )
        recent_run = await database.create_run("scheduled", 1)
        assert recent_run is not None
        await database.complete_run(recent_run.id)

        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=3,
                chat_title="Test",
                sent_at=old + timedelta(minutes=1),
                text="Old failed",
            )
        )
        failed_run = await database.create_run("scheduled", 1)
        assert failed_run is not None
        await database.fail_run(failed_run.id, "temporary")
        await database._write(
            "UPDATE summary_runs SET created_at = ? WHERE id = ?",
            (old.isoformat(), failed_run.id),
        )

        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=4,
                chat_title="Test",
                sent_at=old + timedelta(minutes=2),
                text="Old unsummarized",
            )
        )

        result = await database.run_maintenance(90, vacuum=True, now=now)

        assert result.runs_deleted == 1
        assert result.messages_deleted == 1
        assert result.vacuumed is True
        assert await database.get_run(old_run.id) is None
        assert await database.get_run(recent_run.id) is not None
        assert await database.get_run(failed_run.id) is not None
        assert (
            await database._fetchone(
                "SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ?",
                (-1001, 1),
            )
            is None
        )
        for message_id in (2, 3, 4):
            assert (
                await database._fetchone(
                    "SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ?",
                    (-1001, message_id),
                )
                is not None
            )
        assert await database.unsummarized_count() == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_regeneration_can_reset_stale_delivery_receipts(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    try:
        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=1,
                chat_title="Test",
                sent_at=datetime.now(UTC),
                text="Update",
            )
        )
        run = await database.create_run("scheduled", 1)
        assert run is not None
        await database.save_generated(run.id, DigestResult(), ["old part"])
        await database.record_delivery(run.id, 55)

        await database.save_generated(
            run.id,
            DigestResult(),
            ["new part one", "new part two"],
            reset_delivery=True,
        )

        stored = await database.get_run(run.id)
        assert stored is not None
        assert stored.rendered_parts == ["new part one", "new part two"]
        assert stored.telegram_message_ids == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_connect_migrates_existing_summary_runs_table(tmp_path) -> None:
    path = tmp_path / "digest.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE summary_runs (
            id TEXT PRIMARY KEY,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            window_start TEXT,
            window_end TEXT,
            digest_json TEXT,
            rendered_json TEXT NOT NULL DEFAULT '[]',
            telegram_message_ids_json TEXT NOT NULL DEFAULT '[]',
            error TEXT
        )
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    await database.connect()
    try:
        rows = await database._fetchall("PRAGMA table_info(summary_runs)")
        columns = {row["name"]: row for row in rows}
        assert "failure_count" in columns
        assert columns["failure_count"]["dflt_value"] == "0"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_quarantine_preserves_messages_and_allows_newer_run(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    try:
        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=1,
                chat_title="Test",
                sent_at=datetime(2026, 8, 27, 8, tzinfo=UTC),
                text="Poison batch",
            )
        )
        run = await database.create_run("scheduled", 1)
        assert run is not None

        first = await database.fail_run(run.id, "ValidationError", max_failures=2)
        second = await database.fail_run(run.id, "ValidationError", max_failures=2)
        assert first.failure_count == 1
        assert first.quarantined is False
        assert second.failure_count == 2
        assert second.quarantined is True
        stored = await database.get_run(run.id)
        assert stored is not None
        assert stored.status == "quarantined"
        assert stored.failure_count == 2
        assert await database.get_resumable_run() is None
        assert await database.quarantined_count() == 1

        await database.save_message(
            NormalizedMessage(
                chat_id=-1001,
                message_id=2,
                chat_title="Test",
                sent_at=datetime(2026, 8, 27, 9, tzinfo=UTC),
                text="Newer update",
            )
        )
        newer = await database.create_run("scheduled", 10)
        assert newer is not None
        assert [message.message_id for message in await database.get_run_messages(newer.id)] == [2]
        assert await database.unsummarized_count() == 2

        reactivated = await database.reactivate_oldest_quarantined()
        assert reactivated is not None
        assert reactivated.id == run.id
        assert reactivated.status == "failed"
        assert reactivated.failure_count == 0
        resumed = await database.get_resumable_run()
        assert resumed is not None
        assert resumed.id == run.id
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_live_saves_do_not_advance_cursor_but_batches_do(tmp_path) -> None:
    database = Database(tmp_path / "digest.sqlite3")
    await database.connect()
    try:

        def entry(message_id: int) -> NormalizedMessage:
            return NormalizedMessage(
                chat_id=-1001,
                message_id=message_id,
                chat_title="Test",
                sent_at=datetime.now(UTC),
                text=f"message {message_id}",
            )

        assert await database.save_message(entry(9), advance_cursor=False) is True
        assert await database.get_cursor(-1001) == 0
        assert await database.save_messages([entry(7), entry(8), entry(9)]) == 2
        assert await database.get_cursor(-1001) == 9
        assert await database.save_messages([]) == 0
    finally:
        await database.close()
