from types import SimpleNamespace

import pytest

from tg_digest.ingestion import IngestionService, build_permalink


def test_public_permalink() -> None:
    assert build_permalink(-1001234567890, "example", 42) == "https://t.me/example/42"


def test_private_supergroup_permalink() -> None:
    assert build_permalink(-1001234567890, None, 42) == "https://t.me/c/1234567890/42"


def test_basic_group_has_no_permalink() -> None:
    assert build_permalink(-12345, None, 42) is None


class FakeClient:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = messages
        self.calls: list[tuple[object, dict[str, object]]] = []

    def iter_messages(self, entity: object, **kwargs: object):
        self.calls.append((entity, kwargs))

        async def generate():
            for message in self.messages:
                yield message

        return generate()


class FakeDatabase:
    def __init__(self, cursor: int) -> None:
        self.cursor = cursor
        self.saved_ids: list[int] = []
        self.events: list[tuple[str, int]] = []

    async def get_cursor(self, chat_id: int) -> int:
        self.events.append(("cursor", chat_id))
        return self.cursor

    async def save_messages(self, messages: list[SimpleNamespace]) -> int:
        for message in messages:
            self.events.append(("save", message.id))
            self.saved_ids.append(message.id)
        return len(messages)


@pytest.mark.asyncio
async def test_initial_backfill_limits_and_saves_oldest_first(monkeypatch) -> None:
    client = FakeClient([SimpleNamespace(id=5), SimpleNamespace(id=4), SimpleNamespace(id=3)])
    database = FakeDatabase(cursor=0)
    entity = object()
    service = IngestionService(
        client,
        database,
        [],
        initial_backfill_messages=3,  # type: ignore[arg-type]
    )
    service.entities[-1001] = entity

    async def fake_normalize(client, message, chat=None):
        return message

    monkeypatch.setattr("tg_digest.ingestion.normalize_message", fake_normalize)
    await service.catch_up()

    assert client.calls == [(entity, {"limit": 3})]
    assert database.saved_ids == [3, 4, 5]


@pytest.mark.asyncio
async def test_existing_cursor_streams_messages_in_ascending_order(monkeypatch) -> None:
    client = FakeClient([SimpleNamespace(id=4), SimpleNamespace(id=5)])
    database = FakeDatabase(cursor=3)
    entity = object()
    service = IngestionService(client, database, [])  # type: ignore[arg-type]
    service.entities[-1001] = entity

    async def fake_normalize(client, message, chat=None):
        return message

    monkeypatch.setattr("tg_digest.ingestion.normalize_message", fake_normalize)
    await service.catch_up()

    assert client.calls == [(entity, {"min_id": 3, "reverse": True})]
    assert database.saved_ids == [4, 5]


@pytest.mark.asyncio
async def test_catch_up_uses_cursor_snapshot_taken_before_live_handler(monkeypatch) -> None:
    client = FakeClient([SimpleNamespace(id=4), SimpleNamespace(id=5)])
    database = FakeDatabase(cursor=10)
    entity = object()
    service = IngestionService(client, database, [])  # type: ignore[arg-type]
    service.entities[-1001] = entity

    async def fake_normalize(client, message, chat=None):
        return message

    monkeypatch.setattr("tg_digest.ingestion.normalize_message", fake_normalize)
    await service.catch_up({-1001: 3})

    assert client.calls == [(entity, {"min_id": 3, "reverse": True})]
    assert database.saved_ids == [4, 5]


@pytest.mark.asyncio
async def test_catch_up_now_snapshots_all_cursors_before_saving(monkeypatch) -> None:
    client = FakeClient([SimpleNamespace(id=4), SimpleNamespace(id=5)])
    database = FakeDatabase(cursor=3)
    service = IngestionService(client, database, [])  # type: ignore[arg-type]
    service.entities[-1001] = object()
    service.entities[-1002] = object()

    async def fake_normalize(client, message, chat=None):
        return message

    monkeypatch.setattr("tg_digest.ingestion.normalize_message", fake_normalize)
    count = await service.catch_up_now()

    assert count == 4
    assert database.events[:2] == [("cursor", -1001), ("cursor", -1002)]
    assert all(event[0] == "save" for event in database.events[2:])


class LiveClient:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def add_event_handler(self, handler: object, event: object) -> None:
        self.handlers.append(handler)

    def iter_messages(self, entity: object, **kwargs: object):
        async def generate():
            return
            yield  # pragma: no cover

        return generate()


class LiveDatabase:
    def __init__(self) -> None:
        self.saved: list[tuple[object, bool]] = []

    async def get_cursor(self, chat_id: int) -> int:
        return 7

    async def save_messages(self, messages: list[object]) -> int:
        return 0

    async def save_message(self, message: object, *, advance_cursor: bool = True) -> bool:
        self.saved.append((message, advance_cursor))
        return True


@pytest.mark.asyncio
async def test_live_events_reuse_entity_and_never_advance_cursor(monkeypatch) -> None:
    client = LiveClient()
    database = LiveDatabase()
    entity = object()
    service = IngestionService(client, database, [])  # type: ignore[arg-type]
    service.entities[-1001] = entity
    normalized_with: list[object] = []

    async def no_resolve() -> None:
        return None

    async def fake_normalize(client, message, chat=None):
        normalized_with.append(chat)
        return message

    monkeypatch.setattr(service, "_resolve_groups", no_resolve)
    monkeypatch.setattr("tg_digest.ingestion.normalize_message", fake_normalize)
    await service.start()
    handler = client.handlers[0]

    await handler(SimpleNamespace(chat_id=-1001, message="live"))  # type: ignore[operator]
    await handler(SimpleNamespace(chat_id=-1999, message="other"))  # type: ignore[operator]

    assert normalized_with == [entity]
    assert database.saved == [("live", False)]
