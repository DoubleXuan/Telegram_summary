import json
from datetime import UTC, datetime

from tg_digest.models import NormalizedMessage
from tg_digest.preprocess import prepare_messages
from tg_digest.settings import GroupConfig


def message(message_id: int, text: str, reply_to: int | None = None) -> NormalizedMessage:
    return NormalizedMessage(
        chat_id=-1001,
        message_id=message_id,
        chat_title="Test",
        sent_at=datetime(2026, 8, 24, 9, message_id, tzinfo=UTC),
        text=text,
        reply_to_message_id=reply_to,
    )


def test_filters_low_value_duplicates_and_ignored_keywords() -> None:
    group = GroupConfig(
        name="Test",
        chat_id=-1001,
        keywords=["发布"],
        ignore_keywords=["广告"],
    )
    batch = prepare_messages(
        [
            message(1, "👍"),
            message(2, "收到"),
            message(3, "版本 2.0 将在周五发布"),
            message(4, "版本 2.0 将在周五发布"),
            message(5, "广告：买一送一"),
            message(6, "版本 2.0 将在周五发布", reply_to=3),
        ],
        {-1001: group},
        chunk_max_chars=10_000,
    )
    assert [item.message_id for item in batch.messages] == [3, 6]
    assert batch.original_count == 6
    assert batch.filtered_count == 4
    assert len(batch.chats) == 1
    assert len(batch.chats[0].chunks) == 1
    assert '"message_id":3' in batch.chats[0].chunks[0].records


def test_cross_chat_duplicates_survive_but_same_chat_duplicates_do_not() -> None:
    original = message(1, "API price changed")
    other = original.model_copy(update={"chat_id": -1002, "message_id": 2})
    duplicate = message(3, "API price changed")
    batch = prepare_messages([original, other, duplicate], {}, chunk_max_chars=10_000)
    assert {entry.source_key for entry in batch.messages} == {(-1001, 1), (-1002, 2)}
    assert batch.filtered_count == 1
    assert len(batch.chats) == 2


def test_chunks_are_chat_isolated_with_exact_sources_and_context() -> None:
    inputs = [
        message(index, f"Test report {index}").model_copy(
            update={"chat_id": -1001 if index % 2 else -1002}
        )
        for index in range(1, 7)
    ]
    group = GroupConfig(name="API", chat_id=-1002, topics=["API"], keywords=["Test"])
    batch = prepare_messages(inputs, {-1002: group}, chunk_max_chars=700)
    assert len(batch.chats) == 2
    for chat in batch.chats:
        keys = []
        for chunk in chat.chunks:
            records = [json.loads(line) for line in chunk.records.splitlines()]
            assert all(record["source"]["chat_id"] == chat.chat_id for record in records)
            assert [(s.chat_id, s.message_id) for s in chunk.sources] == [
                (record["source"]["chat_id"], record["source"]["message_id"]) for record in records
            ]
            keys.extend(source.message_id for source in chunk.sources)
        assert keys == sorted(keys)
        assert chat.original_count == chat.kept_count == 3
        if chat.chat_id == -1002:
            assert chat.chat_title == "Test"
            assert chat.topics == ["API"]
            assert chat.keywords == ["Test"]


def test_all_filtered_chat_is_counted_without_chunks() -> None:
    batch = prepare_messages([message(1, "👍"), message(2, "收到")], {}, chunk_max_chars=1000)
    assert batch.messages == []
    assert batch.chats[0].chunks == []
    assert batch.chats[0].original_count == batch.filtered_count == 2
    assert batch.chats[0].kept_count == 0


def test_oversized_record_remains_whole_in_own_chunk() -> None:
    batch = prepare_messages([message(1, "Report " * 1000)], {}, chunk_max_chars=1000)
    assert len(batch.chats[0].chunks) == 1
    assert len(batch.chats[0].chunks[0].records) > 1000
    assert batch.chats[0].chunks[0].sources[0].message_id == 1


def test_records_omit_chat_context_and_empty_fields() -> None:
    entry = message(1, "see https://a.example").model_copy(
        update={"urls": ["https://a.example", "https://hidden.example"], "sender_name": "Ann"}
    )
    group = GroupConfig(name="AI", chat_id=-1001, topics=["API"], keywords=["see"])
    batch = prepare_messages([entry], {-1001: group}, 10_000, timezone="Asia/Shanghai")
    record = json.loads(batch.chats[0].chunks[0].records)
    assert record == {
        "source": {"chat_id": -1001, "message_id": 1},
        "sent_at": "2026-08-24 17:01",
        "sender": "Ann",
        "urls": ["https://hidden.example"],
        "text": "see https://a.example",
    }
    assert batch.timezone == "Asia/Shanghai"
