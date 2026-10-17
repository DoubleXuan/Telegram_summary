from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .models import DigestResult, NormalizedMessage, SummaryRun

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    chat_title TEXT NOT NULL,
    chat_username TEXT,
    sender_name TEXT,
    sent_at TEXT NOT NULL,
    text TEXT NOT NULL,
    reply_to_message_id INTEGER,
    forwarded_from TEXT,
    media_type TEXT,
    urls_json TEXT NOT NULL DEFAULT '[]',
    permalink TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    summarized_at TEXT,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_unsummarized
    ON messages(summarized_at, sent_at);

CREATE TABLE IF NOT EXISTS ingestion_cursors (
    chat_id INTEGER PRIMARY KEY,
    last_message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_runs (
    id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    digest_json TEXT,
    rendered_json TEXT NOT NULL DEFAULT '[]',
    telegram_message_ids_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_summary_runs_status
    ON summary_runs(status, created_at);

CREATE TABLE IF NOT EXISTS summary_run_messages (
    run_id TEXT NOT NULL REFERENCES summary_runs(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (run_id, chat_id, message_id),
    FOREIGN KEY (chat_id, message_id) REFERENCES messages(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_summary_run_messages_message
    ON summary_run_messages(chat_id, message_id);
"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    runs_deleted: int
    messages_deleted: int
    vacuumed: bool


@dataclass(frozen=True, slots=True)
class RunFailureResult:
    failure_count: int
    quarantined: bool


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.connection is not None:
            return
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript(SCHEMA)
        await self._migrate_schema()
        await self.connection.commit()

    async def _migrate_schema(self) -> None:
        conn = self._conn()
        cursor = await conn.execute("PRAGMA table_info(summary_runs)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        await cursor.close()
        if "failure_count" not in columns:
            await conn.execute(
                "ALTER TABLE summary_runs ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
            )

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    def _conn(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("database is not connected")
        return self.connection

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            conn = self._conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def _write(self, query: str, parameters: tuple[Any, ...]) -> None:
        async with self._write_lock:
            conn = self._conn()
            await conn.execute(query, parameters)
            await conn.commit()

    async def _fetchone(self, query: str, parameters: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self._write_lock:
            cursor = await self._conn().execute(query, parameters)
            return await cursor.fetchone()

    async def _fetchall(self, query: str, parameters: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self._write_lock:
            cursor = await self._conn().execute(query, parameters)
            return await cursor.fetchall()

    async def get_cursor(self, chat_id: int) -> int:
        row = await self._fetchone(
            "SELECT last_message_id FROM ingestion_cursors WHERE chat_id = ?", (chat_id,)
        )
        return int(row["last_message_id"]) if row else 0

    async def save_message(
        self, message: NormalizedMessage, *, advance_cursor: bool = True
    ) -> bool:
        return await self.save_messages([message], advance_cursor=advance_cursor) == 1

    async def save_messages(
        self, messages: Sequence[NormalizedMessage], *, advance_cursor: bool = True
    ) -> int:
        """Insert messages idempotently and return how many were new.

        The ingestion cursor must only advance for messages fetched in contiguous ID order;
        live events can arrive with gaps, so they pass ``advance_cursor=False``.
        """
        if not messages:
            return 0
        now = _utc_now().isoformat()
        inserted = 0
        async with self._transaction() as conn:
            chats = {message.chat_id: message for message in messages}
            await conn.executemany(
                """
                INSERT INTO chats(chat_id, title, username, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    username = excluded.username,
                    updated_at = excluded.updated_at
                """,
                [
                    (message.chat_id, message.chat_title, message.chat_username, now)
                    for message in chats.values()
                ],
            )
            for message in messages:
                result = await conn.execute(
                    """
                    INSERT OR IGNORE INTO messages(
                        chat_id, message_id, chat_title, chat_username, sender_name, sent_at,
                        text, reply_to_message_id, forwarded_from, media_type, urls_json,
                        permalink, is_admin
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.chat_id,
                        message.message_id,
                        message.chat_title,
                        message.chat_username,
                        message.sender_name,
                        message.sent_at.isoformat(),
                        message.text,
                        message.reply_to_message_id,
                        message.forwarded_from,
                        message.media_type,
                        json.dumps(message.urls, ensure_ascii=False),
                        message.permalink,
                        int(message.is_admin),
                    ),
                )
                inserted += result.rowcount == 1
            if advance_cursor:
                latest: dict[int, int] = {}
                for message in messages:
                    latest[message.chat_id] = max(
                        latest.get(message.chat_id, 0), message.message_id
                    )
                await conn.executemany(
                    """
                    INSERT INTO ingestion_cursors(chat_id, last_message_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        last_message_id = MAX(last_message_id, excluded.last_message_id),
                        updated_at = excluded.updated_at
                    """,
                    [(chat_id, message_id, now) for chat_id, message_id in latest.items()],
                )
        return inserted

    async def get_resumable_run(self) -> SummaryRun | None:
        row = await self._fetchone(
            """
            SELECT * FROM summary_runs
            WHERE status IN ('prepared', 'generated', 'delivering', 'failed')
            ORDER BY created_at ASC LIMIT 1
            """
        )
        return self._row_to_run(row) if row else None

    async def create_run(self, trigger: str, limit: int) -> SummaryRun | None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT m.chat_id, m.message_id, m.sent_at
                FROM messages m
                WHERE m.summarized_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM summary_run_messages srm
                    JOIN summary_runs sr ON sr.id = srm.run_id
                    WHERE srm.chat_id = m.chat_id
                      AND srm.message_id = m.message_id
                      AND sr.status IN (
                          'prepared', 'generated', 'delivering', 'failed', 'quarantined'
                      )
                  )
                ORDER BY m.sent_at, m.chat_id, m.message_id
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return None

            run_id = uuid.uuid4().hex[:16]
            created_at = _utc_now()
            window_start = min(datetime.fromisoformat(row["sent_at"]) for row in rows)
            window_end = max(datetime.fromisoformat(row["sent_at"]) for row in rows)
            await conn.execute(
                """
                INSERT INTO summary_runs(
                    id, trigger, status, created_at, window_start, window_end
                ) VALUES (?, ?, 'prepared', ?, ?, ?)
                """,
                (
                    run_id,
                    trigger,
                    created_at.isoformat(),
                    window_start.isoformat(),
                    window_end.isoformat(),
                ),
            )
            await conn.executemany(
                """
                INSERT INTO summary_run_messages(run_id, chat_id, message_id)
                VALUES (?, ?, ?)
                """,
                [(run_id, row["chat_id"], row["message_id"]) for row in rows],
            )
            return SummaryRun(
                id=run_id,
                trigger=trigger,
                status="prepared",
                created_at=created_at,
                window_start=window_start,
                window_end=window_end,
            )

    async def get_run_messages(self, run_id: str) -> list[NormalizedMessage]:
        rows = await self._fetchall(
            """
            SELECT m.*
            FROM messages m
            JOIN summary_run_messages srm
              ON srm.chat_id = m.chat_id AND srm.message_id = m.message_id
            WHERE srm.run_id = ?
            ORDER BY m.sent_at, m.chat_id, m.message_id
            """,
            (run_id,),
        )
        return [self._row_to_message(row) for row in rows]

    async def save_generated(
        self,
        run_id: str,
        digest: DigestResult,
        rendered_parts: list[str],
        *,
        reset_delivery: bool = False,
    ) -> None:
        await self._write(
            """
            UPDATE summary_runs
            SET status = 'generated',
                digest_json = ?,
                rendered_json = ?,
                telegram_message_ids_json = CASE WHEN ? THEN '[]'
                    ELSE telegram_message_ids_json END,
                error = NULL
            WHERE id = ?
            """,
            (
                digest.model_dump_json(),
                json.dumps(rendered_parts, ensure_ascii=False),
                int(reset_delivery),
                run_id,
            ),
        )

    async def mark_delivering(self, run_id: str) -> None:
        await self._write(
            "UPDATE summary_runs SET status = 'delivering', error = NULL WHERE id = ?",
            (run_id,),
        )

    async def record_delivery(self, run_id: str, telegram_message_id: int) -> None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "SELECT telegram_message_ids_json FROM summary_runs WHERE id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(run_id)
            ids = [*json.loads(row["telegram_message_ids_json"]), telegram_message_id]
            await conn.execute(
                "UPDATE summary_runs SET telegram_message_ids_json = ? WHERE id = ?",
                (json.dumps(ids), run_id),
            )

    async def complete_run(self, run_id: str) -> None:
        now = _utc_now().isoformat()
        async with self._transaction() as conn:
            await conn.execute(
                """
                UPDATE messages
                SET summarized_at = ?
                WHERE EXISTS (
                    SELECT 1 FROM summary_run_messages srm
                    WHERE srm.run_id = ?
                      AND srm.chat_id = messages.chat_id
                      AND srm.message_id = messages.message_id
                )
                """,
                (now, run_id),
            )
            await conn.execute(
                "UPDATE summary_runs SET status = 'delivered', error = NULL WHERE id = ?",
                (run_id,),
            )

    async def fail_run(
        self,
        run_id: str,
        error: str,
        max_failures: int = 3,
    ) -> RunFailureResult:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "SELECT failure_count FROM summary_runs WHERE id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise KeyError(run_id)

            failure_count = int(row["failure_count"]) + 1
            quarantined = failure_count >= max_failures
            await conn.execute(
                """
                UPDATE summary_runs
                SET status = ?, error = ?, failure_count = ?
                WHERE id = ?
                """,
                (
                    "quarantined" if quarantined else "failed",
                    error[:500],
                    failure_count,
                    run_id,
                ),
            )
            return RunFailureResult(
                failure_count=failure_count,
                quarantined=quarantined,
            )

    async def quarantined_count(self) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS count FROM summary_runs WHERE status = 'quarantined'"
        )
        return int(row["count"]) if row else 0

    async def reactivate_oldest_quarantined(self) -> SummaryRun | None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT id FROM summary_runs
                WHERE status = 'quarantined'
                ORDER BY created_at ASC LIMIT 1
                """
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None

            run_id = str(row["id"])
            await conn.execute(
                """
                UPDATE summary_runs
                SET status = 'failed', error = NULL, failure_count = 0
                WHERE id = ?
                """,
                (run_id,),
            )
            cursor = await conn.execute(
                "SELECT * FROM summary_runs WHERE id = ?",
                (run_id,),
            )
            updated = await cursor.fetchone()
            await cursor.close()
            return self._row_to_run(updated) if updated else None

    async def get_run(self, run_id: str) -> SummaryRun | None:
        row = await self._fetchone("SELECT * FROM summary_runs WHERE id = ?", (run_id,))
        return self._row_to_run(row) if row else None

    async def unsummarized_count(self) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS count FROM messages WHERE summarized_at IS NULL"
        )
        if row is None:
            return 0
        return int(row["count"])

    async def run_maintenance(
        self,
        retention_days: int,
        *,
        vacuum: bool = True,
        now: datetime | None = None,
    ) -> MaintenanceResult:
        reference_time = now or _utc_now()
        if reference_time.tzinfo is None:
            raise ValueError("maintenance reference time must be timezone-aware")
        cutoff = (reference_time.astimezone(UTC) - timedelta(days=retention_days)).isoformat()

        async with self._write_lock:
            conn = self._conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                run_cursor = await conn.execute(
                    """
                    DELETE FROM summary_runs
                    WHERE status = 'delivered'
                      AND created_at < ?
                    """,
                    (cutoff,),
                )
                message_cursor = await conn.execute(
                    """
                    DELETE FROM messages
                    WHERE sent_at < ?
                      AND summarized_at IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1
                        FROM summary_run_messages srm
                        WHERE srm.chat_id = messages.chat_id
                          AND srm.message_id = messages.message_id
                      )
                    """,
                    (cutoff,),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

            runs_deleted = max(run_cursor.rowcount, 0)
            messages_deleted = max(message_cursor.rowcount, 0)
            await run_cursor.close()
            await message_cursor.close()
            checkpoint_cursor = await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await checkpoint_cursor.fetchall()
            await checkpoint_cursor.close()

            vacuumed = False
            if vacuum and (runs_deleted or messages_deleted):
                await conn.execute("VACUUM")
                vacuumed = True

            return MaintenanceResult(
                runs_deleted=runs_deleted,
                messages_deleted=messages_deleted,
                vacuumed=vacuumed,
            )

    @staticmethod
    def _row_to_message(row: aiosqlite.Row) -> NormalizedMessage:
        return NormalizedMessage(
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            chat_title=row["chat_title"],
            chat_username=row["chat_username"],
            sender_name=row["sender_name"],
            sent_at=datetime.fromisoformat(row["sent_at"]),
            text=row["text"],
            reply_to_message_id=row["reply_to_message_id"],
            forwarded_from=row["forwarded_from"],
            media_type=row["media_type"],
            urls=json.loads(row["urls_json"]),
            permalink=row["permalink"],
            is_admin=bool(row["is_admin"]),
        )

    @staticmethod
    def _row_to_run(row: aiosqlite.Row) -> SummaryRun:
        digest = (
            DigestResult.model_validate_json(row["digest_json"]) if row["digest_json"] else None
        )
        data: dict[str, Any] = {
            "id": row["id"],
            "trigger": row["trigger"],
            "status": row["status"],
            "created_at": datetime.fromisoformat(row["created_at"]),
            "window_start": _dt(row["window_start"]),
            "window_end": _dt(row["window_end"]),
            "digest": digest,
            "rendered_parts": json.loads(row["rendered_json"]),
            "telegram_message_ids": json.loads(row["telegram_message_ids_json"]),
            "error": row["error"],
            "failure_count": row["failure_count"],
        }
        return SummaryRun.model_validate(data)
