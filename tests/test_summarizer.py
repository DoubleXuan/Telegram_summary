import json
from datetime import UTC, datetime

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from tg_digest.models import (
    DigestResult,
    NormalizedMessage,
    Priority,
    SourceRef,
    SummaryCategory,
    SummaryItem,
)
from tg_digest.preprocess import prepare_messages
from tg_digest.settings import GroupConfig, SummaryConfig
from tg_digest.summarizer import AnthropicSummarizer, parse_digest_result, validate_digest


def source_message(message_id: int) -> NormalizedMessage:
    return NormalizedMessage(
        chat_id=-1001,
        message_id=message_id,
        chat_title="Test",
        sent_at=datetime.now(UTC),
        text="important",
    )


def item(priority: Priority, message_id: int) -> SummaryItem:
    return SummaryItem(
        title=f"Item {message_id}",
        summary="Summary",
        priority=priority,
        category=SummaryCategory.MUST_KNOW,
        sources=[SourceRef(chat_id=-1001, message_id=message_id)],
    )


def bad_request() -> anthropic.BadRequestError:
    request = httpx.Request("POST", "http://localhost:8317/v1/messages")
    response = httpx.Response(400, request=request)
    return anthropic.BadRequestError("unsupported parameter", response=response, body={})


def test_validate_digest_removes_invalid_p3_and_extra_p2() -> None:
    digest = DigestResult(
        items=[
            item(Priority.P2, 1),
            item(Priority.P2, 2),
            item(Priority.P1, 999),
            item(Priority.P3, 1),
            item(Priority.P0, 2),
        ]
    )
    validated = validate_digest(digest, [source_message(1), source_message(2)], max_p2_items=1)
    assert [entry.priority for entry in validated.items] == [Priority.P0, Priority.P2]
    assert [entry.sources[0].message_id for entry in validated.items] == [2, 1]


def test_parse_digest_result_accepts_wrapped_json() -> None:
    payload = DigestResult(overview="ok", omitted_count=3).model_dump_json()
    parsed = parse_digest_result(f"Result follows:\n```json\n{payload}\n```\nDone")
    assert parsed.overview == "ok"
    assert parsed.omitted_count == 3


def test_parse_digest_result_rejects_invalid_output() -> None:
    with pytest.raises(ValueError, match="valid JSON object"):
        parse_digest_result("not json")
    with pytest.raises(ValidationError):
        parse_digest_result('{"overview": 123, "unexpected": true}')


@pytest.mark.asyncio
async def test_summarizer_accepts_auth_token_and_base_url() -> None:
    summarizer = AnthropicSummarizer(
        SummaryConfig(),
        auth_token="gateway-token",
        base_url="http://localhost:8317",
        model="gpt-5.5",
    )
    assert str(summarizer.client.base_url) == "http://localhost:8317"
    assert summarizer.model == "gpt-5.5"
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_uses_advanced_mode_when_gateway_supports_it(monkeypatch) -> None:
    summarizer = AnthropicSummarizer(SummaryConfig(), api_key="api-key")
    calls: list[bool] = []
    payload = DigestResult(overview="advanced").model_dump_json()

    async def fake_stream(prompt: str, *, advanced: bool) -> str:
        calls.append(advanced)
        return payload

    monkeypatch.setattr(summarizer, "_stream_text", fake_stream)
    result = await summarizer._request("summarize")

    assert result.overview == "advanced"
    assert calls == [True]
    assert summarizer._advanced_api_supported is True
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_forced_compatibility_mode_skips_advanced_request(monkeypatch) -> None:
    summarizer = AnthropicSummarizer(
        SummaryConfig(),
        auth_token="gateway-token",
        compatibility_mode=True,
    )
    calls: list[bool] = []
    payload = DigestResult(overview="compatibility").model_dump_json()

    async def fake_stream(prompt: str, *, advanced: bool) -> str:
        calls.append(advanced)
        assert "required_json_schema" in prompt
        return payload

    monkeypatch.setattr(summarizer, "_stream_text", fake_stream)
    result = await summarizer._request("summarize")

    assert result.overview == "compatibility"
    assert calls == [False]
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_falls_back_once_after_gateway_rejects_advanced_request(monkeypatch) -> None:
    summarizer = AnthropicSummarizer(
        SummaryConfig(),
        auth_token="gateway-token",
        base_url="http://localhost:8317",
        model="gpt-5.5",
    )
    calls: list[bool] = []
    payload = DigestResult(overview="fallback").model_dump_json()

    async def fake_stream(prompt: str, *, advanced: bool) -> str:
        calls.append(advanced)
        if advanced:
            raise bad_request()
        assert "required_json_schema" in prompt
        return payload

    monkeypatch.setattr(summarizer, "_stream_text", fake_stream)
    first = await summarizer._request("summarize")
    second = await summarizer._request("summarize again")

    assert first.overview == "fallback"
    assert second.overview == "fallback"
    assert calls == [True, False, False]
    assert summarizer._advanced_api_supported is False
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_falls_back_when_gateway_ignores_structured_output(monkeypatch) -> None:
    summarizer = AnthropicSummarizer(SummaryConfig(), auth_token="gateway-token")
    calls: list[bool] = []
    payload = DigestResult(overview="valid JSON").model_dump_json()

    async def fake_stream(prompt: str, *, advanced: bool) -> str:
        calls.append(advanced)
        return "schema ignored" if advanced else f"```json\n{payload}\n```"

    monkeypatch.setattr(summarizer, "_stream_text", fake_stream)
    result = await summarizer._request("summarize")

    assert result.overview == "valid JSON"
    assert calls == [True, False]
    await summarizer.client.close()


def test_parse_reports_complete_digest_error_instead_of_nested_source() -> None:
    payload = """{
        "overview": "",
        "items": [{
            "title": "Important",
            "summary": "Act now",
            "priority": "P1",
            "category": "action",
            "sources": [{"chat_id": -1001, "message_id": 7}],
            "chat_id": -1001,
            "message_id": 7
        }],
        "omitted_count": 0
    }"""

    with pytest.raises(ValidationError) as error:
        parse_digest_result(payload)

    locations = [entry["loc"] for entry in error.value.errors()]
    assert ("items", 0, "chat_id") in locations
    assert ("items", 0, "message_id") in locations
    assert ("chat_id",) not in locations


def test_parse_skips_invalid_fragment_and_finds_complete_digest() -> None:
    valid = DigestResult(overview="recovered").model_dump_json()
    parsed = parse_digest_result(f'Unrelated: {{"chat_id": -1001}}\nResult: {valid}')
    assert parsed.overview == "recovered"


@pytest.mark.asyncio
async def test_compatibility_mode_repairs_malformed_output_once(monkeypatch) -> None:
    summarizer = AnthropicSummarizer(
        SummaryConfig(output_repair_attempts=1),
        auth_token="gateway-token",
        compatibility_mode=True,
    )
    malformed = '{"overview":"bad","items":[],"chat_id":-1001}'
    repaired = DigestResult(overview="repaired").model_dump_json()
    prompts: list[str] = []

    async def fake_stream(prompt: str, *, advanced: bool) -> str:
        assert advanced is False
        prompts.append(prompt)
        return malformed if len(prompts) == 1 else repaired

    monkeypatch.setattr(summarizer, "_stream_text", fake_stream)
    result = await summarizer._request("summarize")

    assert result.overview == "repaired"
    assert len(prompts) == 2
    assert "telegram_records" not in prompts[1]
    assert "invalid_model_output" in prompts[1]
    assert malformed in prompts[1]
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_compatibility_mode_raises_after_repair_limit(monkeypatch) -> None:
    summarizer = AnthropicSummarizer(
        SummaryConfig(output_repair_attempts=1),
        auth_token="gateway-token",
        compatibility_mode=True,
    )
    malformed = '{"overview":"bad","items":[],"chat_id":-1001}'
    calls = 0

    async def fake_stream(prompt: str, *, advanced: bool) -> str:
        nonlocal calls
        calls += 1
        return malformed

    monkeypatch.setattr(summarizer, "_stream_text", fake_stream)

    with pytest.raises(ValidationError):
        await summarizer._request("summarize")
    assert calls == 2
    await summarizer.client.close()


def group_message(chat_id: int, message_id: int = 1) -> NormalizedMessage:
    return source_message(message_id).model_copy(
        update={
            "chat_id": chat_id,
            "text": "private-body-sentinel",
            "sender_name": "sender-sentinel",
        }
    )


def group_item(chat_id: int, message_id: int = 1, priority: Priority = Priority.P2) -> SummaryItem:
    return item(priority, message_id).model_copy(
        update={
            "title": f"Finding {chat_id}/{message_id}",
            "summary": "private-candidate-sentinel",
            "sources": [SourceRef(chat_id=chat_id, message_id=message_id)],
            "unconfirmed": True,
        }
    )


def prompt_data(prompt: str, tag: str):
    return json.loads(prompt.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0])


@pytest.mark.asyncio
async def test_multi_chat_extraction_restores_missing_chat_without_changing_model(
    monkeypatch, caplog
):
    messages = [group_message(chat_id) for chat_id in (-1001, -1002, -1003, -1004)]
    group = GroupConfig(name="AI", chat_id=-1004, topics=["API tests"], keywords=["failure"])
    batch = prepare_messages(messages, {-1004: group}, 10000)
    summarizer = AnthropicSummarizer(
        SummaryConfig(max_p2_items=10), api_key="key", model="gpt-6-sol"
    )
    calls = []
    caplog.set_level("INFO")

    async def fake_request(prompt):
        calls.append(prompt)
        if "<telegram_records>" in prompt:
            record = prompt_data(prompt, "telegram_records")
            return DigestResult(items=[group_item(record["source"]["chat_id"])])
        groups = prompt_data(prompt, "candidates")
        assert len(groups) == 4
        assert groups[-1]["topics"] == ["API tests"]
        return DigestResult(items=[SummaryItem.model_validate(groups[0]["items"][0])])

    monkeypatch.setattr(summarizer, "_request", fake_request)
    result = await summarizer.summarize(batch, run_id="synthetic-run")
    assert len(calls) == 5
    assert {s.chat_id for entry in result.items for s in entry.sources} == {
        -1001,
        -1002,
        -1003,
        -1004,
    }
    assert len(result.items) == 4
    assert all(entry.unconfirmed for entry in result.items)
    assert result.omitted_count == 0
    assert summarizer.model == "gpt-6-sol"
    assert "chat_id=-1004 candidates=1 merged=0 restored=1 final=1" in caplog.text
    assert "private-body-sentinel" not in caplog.text
    assert "private-candidate-sentinel" not in caplog.text
    assert "sender-sentinel" not in caplog.text
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_small_single_chat_uses_direct_and_computes_omissions(monkeypatch):
    batch = prepare_messages([group_message(-1001), group_message(-1001, 2)], {}, 10000)
    summarizer = AnthropicSummarizer(SummaryConfig(), api_key="key")
    calls = []

    async def fake_request(prompt):
        calls.append(prompt)
        assert "single group's records" in prompt
        return DigestResult(items=[group_item(-1001)], omitted_count=999)

    monkeypatch.setattr(summarizer, "_request", fake_request)
    result = await summarizer.summarize(batch)
    assert len(calls) == 1
    assert len(result.items) == 1
    assert result.omitted_count == 0  # same-chat identical text was filtered before the model
    assert batch.filtered_count == 1
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_candidates_are_not_capped_before_merge_and_p2_diversity_is_global(monkeypatch):
    messages = [
        group_message(-1001, index).model_copy(update={"text": f"Test {index}"})
        for index in range(1, 13)
    ]
    messages += [group_message(chat) for chat in (-1002, -1003, -1004)]
    batch = prepare_messages(messages, {}, 100000)
    summarizer = AnthropicSummarizer(SummaryConfig(max_p2_items=10), api_key="key")
    calls = 0

    async def fake_request(prompt):
        nonlocal calls
        calls += 1
        if "<telegram_records>" in prompt:
            records = [
                json.loads(line)
                for line in prompt.split("<telegram_records>\n")[1]
                .split("\n</telegram_records>")[0]
                .splitlines()
            ]
            return DigestResult(
                items=[
                    group_item(r["source"]["chat_id"], r["source"]["message_id"]) for r in records
                ]
            )
        groups = prompt_data(prompt, "candidates")
        assert len(groups[0]["items"]) == 12
        return DigestResult(
            items=[SummaryItem.model_validate(entry) for entry in groups[0]["items"]]
        )

    monkeypatch.setattr(summarizer, "_request", fake_request)
    result = await summarizer.summarize(batch)
    assert calls == 5
    assert len(result.items) == 10
    assert all(entry.priority == Priority.P2 for entry in result.items)
    assert {s.chat_id for entry in result.items for s in entry.sources} == {
        -1001,
        -1002,
        -1003,
        -1004,
    }
    assert result.omitted_count == 5
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_invalid_and_p3_candidates_never_enter_merge_or_fallback(monkeypatch):
    messages = [group_message(chat) for chat in (-1001, -1002, -1003)]
    batch = prepare_messages(messages, {}, 10000)
    summarizer = AnthropicSummarizer(SummaryConfig(), api_key="key")

    async def fake_request(prompt):
        if "<telegram_records>" in prompt:
            chat = prompt_data(prompt, "telegram_records")["source"]["chat_id"]
            if chat == -1001:
                return DigestResult(items=[group_item(chat)])
            if chat == -1002:
                contaminated = group_item(chat).model_copy(
                    update={
                        "sources": [
                            SourceRef(chat_id=chat, message_id=1),
                            SourceRef(chat_id=-1001, message_id=1),
                        ]
                    }
                )
                return DigestResult(items=[contaminated])
            return DigestResult(items=[group_item(chat, priority=Priority.P3)])
        groups = prompt_data(prompt, "candidates")
        assert [len(group["items"]) for group in groups] == [1, 0, 0]
        return DigestResult(items=[group_item(-1001), group_item(-1002)])

    monkeypatch.setattr(summarizer, "_request", fake_request)
    result = await summarizer.summarize(batch)
    assert [entry.sources[0].chat_id for entry in result.items] == [-1001]
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_wrong_chunk_citation_is_rejected_even_in_same_chat(monkeypatch):
    messages = [
        group_message(-1001, index).model_copy(update={"text": str(index) * 1500})
        for index in (1, 2)
    ]
    batch = prepare_messages(messages, {}, 1000)
    summarizer = AnthropicSummarizer(SummaryConfig(direct_summary_max_chars=1000), api_key="key")
    calls = 0

    async def fake_request(prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return DigestResult(items=[group_item(-1001, 2)])
        if calls == 2:
            return DigestResult(items=[group_item(-1001, 2)])
        groups = prompt_data(prompt, "candidates")
        assert len(groups[0]["items"]) == 1
        return DigestResult(items=[group_item(-1001, 2)])

    monkeypatch.setattr(summarizer, "_request", fake_request)
    result = await summarizer.summarize(batch)
    assert calls == 3
    assert result.omitted_count == 1
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_all_filtered_batch_makes_no_requests(monkeypatch):
    batch = prepare_messages([group_message(-1001).model_copy(update={"text": "👍"})], {}, 1000)
    summarizer = AnthropicSummarizer(SummaryConfig(), api_key="key")

    async def unexpected_request(prompt):
        pytest.fail("all-filtered batch must not call model")

    monkeypatch.setattr(summarizer, "_request", unexpected_request)
    result = await summarizer.summarize(batch)
    assert result.items == []
    assert result.omitted_count == 0
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_merge_cannot_rewrite_facts_or_relabel_uncertainty(monkeypatch):
    batch = prepare_messages([group_message(-1001), group_message(-1002)], {}, 10000)
    summarizer = AnthropicSummarizer(SummaryConfig(), api_key="key")

    async def fake_request(prompt):
        if "<telegram_records>" in prompt:
            chat = prompt_data(prompt, "telegram_records")["source"]["chat_id"]
            return DigestResult(items=[group_item(chat, priority=Priority.P1)])
        return DigestResult(
            items=[
                group_item(-1001, priority=Priority.P1).model_copy(
                    update={"summary": "invented rewrite", "unconfirmed": False}
                )
            ]
        )

    monkeypatch.setattr(summarizer, "_request", fake_request)
    result = await summarizer.summarize(batch)
    assert len(result.items) == 2
    assert all(entry.summary == "private-candidate-sentinel" for entry in result.items)
    assert all(entry.unconfirmed for entry in result.items)
    await summarizer.client.close()


def test_global_p2_selection_preserves_important_and_distinct_same_source_facts():
    messages = [group_message(chat, index) for chat in (-1001, -1002) for index in range(1, 13)]
    p2 = [group_item(-1001, index) for index in range(1, 13)] + [group_item(-1002)]
    important = [group_item(-1001, index, Priority.P1) for index in range(1, 13)]
    other_fact = group_item(-1001, 1, Priority.P1).model_copy(update={"summary": "another outcome"})
    digest = DigestResult(items=p2 + important + [other_fact])
    result = validate_digest(digest, messages, max_p2_items=1)
    assert sum(entry.priority == Priority.P1 for entry in result.items) == 13
    assert [
        entry.sources[0].chat_id for entry in result.items if entry.priority == Priority.P2
    ] == [-1002]
    zero = validate_digest(digest, messages, max_p2_items=0)
    assert len(zero.items) == 13
    assert all(entry.priority == Priority.P1 for entry in zero.items)


def test_exact_duplicate_facts_union_cross_chat_citations_and_count_once():
    first = group_item(-1001)
    second = first.model_copy(update={"sources": [SourceRef(chat_id=-1002, message_id=1)]})
    digest = DigestResult(items=[first, second])
    result = validate_digest(digest, [group_message(-1001), group_message(-1002)], max_p2_items=1)
    assert len(result.items) == 1
    assert [s.chat_id for s in result.items[0].sources] == [-1001, -1002]


def test_p2_scarcity_is_capped_and_deterministic():
    messages = [group_message(-1000 - index) for index in range(12)]
    digest = DigestResult(items=[group_item(m.chat_id) for m in messages])
    first = validate_digest(digest, messages, max_p2_items=10)
    second = validate_digest(digest, messages, max_p2_items=10)
    assert first == second
    assert len(first.items) == 10
    assert [entry.sources[0].chat_id for entry in first.items] == [m.chat_id for m in messages[:10]]


@pytest.mark.asyncio
async def test_schema_failure_is_sanitized_before_service_logging(monkeypatch, caplog):
    batch = prepare_messages([group_message(-1001)], {}, 10000)
    summarizer = AnthropicSummarizer(SummaryConfig(), api_key="key")

    async def fake_request(prompt):
        return DigestResult.model_validate({"overview": {"private-schema-sentinel": "secret"}})

    monkeypatch.setattr(summarizer, "_request", fake_request)
    with pytest.raises(ValueError, match="failed schema validation") as error:
        await summarizer.summarize(batch, run_id="test-run")
    assert error.value.__suppress_context__ is True
    assert "private-schema-sentinel" not in str(error.value)
    assert "private-schema-sentinel" not in caplog.text
    assert "error=ValidationError count=1" in caplog.text
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_empty_merge_restores_all_important_candidates(monkeypatch):
    messages = [
        group_message(-1001, index).model_copy(update={"text": f"Test {index}"})
        for index in range(1, 13)
    ]
    messages += [group_message(-1002)]
    batch = prepare_messages(messages, {}, 100000)
    summarizer = AnthropicSummarizer(SummaryConfig(max_p2_items=0), api_key="key")

    async def fake_request(prompt):
        if "<candidates>" in prompt:
            return DigestResult()
        records = [
            json.loads(line)
            for line in prompt.split("<telegram_records>\n")[1]
            .split("\n</telegram_records>")[0]
            .splitlines()
        ]
        return DigestResult(
            items=[
                group_item(
                    r["source"]["chat_id"],
                    r["source"]["message_id"],
                    Priority.P1 if r["source"]["chat_id"] == -1001 else Priority.P2,
                )
                for r in records
            ]
        )

    monkeypatch.setattr(summarizer, "_request", fake_request)
    result = await summarizer.summarize(batch)
    assert len(result.items) == 12
    assert all(entry.priority == Priority.P1 for entry in result.items)
    assert result.omitted_count == 1
    await summarizer.client.close()


def test_source_limit_preserves_group_diversity_for_identical_fact():
    messages = [group_message(-1001, index) for index in range(1, 11)] + [group_message(-1002)]
    first = group_item(-1001).model_copy(
        update={"sources": [SourceRef(chat_id=-1001, message_id=index) for index in range(1, 11)]}
    )
    second = first.model_copy(update={"sources": [SourceRef(chat_id=-1002, message_id=1)]})
    result = validate_digest(DigestResult(items=[first, second]), messages, max_p2_items=1)
    assert len(result.items[0].sources) == 10
    assert {s.chat_id for s in result.items[0].sources} == {-1001, -1002}
    SummaryItem.model_validate(result.items[0].model_dump())


@pytest.mark.asyncio
async def test_grouped_extraction_and_merge_still_use_schema_repair(monkeypatch):
    batch = prepare_messages([group_message(-1001), group_message(-1002)], {}, 10000)
    summarizer = AnthropicSummarizer(SummaryConfig(), api_key="key", compatibility_mode=True)
    malformed = '{"overview":"bad","chat_id":-1001}'
    output = [
        malformed,
        DigestResult(items=[group_item(-1001)]).model_dump_json(),
        DigestResult(items=[group_item(-1002)]).model_dump_json(),
        malformed,
        DigestResult(items=[group_item(-1001), group_item(-1002)]).model_dump_json(),
    ]
    prompts = []

    async def fake_stream(prompt, *, advanced):
        assert advanced is False
        prompts.append(prompt)
        return output[len(prompts) - 1]

    monkeypatch.setattr(summarizer, "_stream_text", fake_stream)
    result = await summarizer.summarize(batch)
    assert len(prompts) == 5
    assert len(result.items) == 2
    assert "telegram_records" not in prompts[1]
    assert "telegram_records" not in prompts[4]
    assert "invalid_model_output" in prompts[1]
    assert "invalid_model_output" in prompts[4]
    await summarizer.client.close()


def test_parser_does_not_accept_nested_empty_object_as_digest():
    with pytest.raises(ValidationError):
        parse_digest_result('{"unexpected":{}}')
    with pytest.raises(ValidationError):
        parse_digest_result('{"unexpected":{"items":[]}}')


def test_validator_recounts_messages_after_budget_and_source_rejection():
    digest = DigestResult(overview="uncited content", items=[group_item(-1001)])
    result = validate_digest(digest, [group_message(-1001)], max_p2_items=0)
    assert result.items == []
    assert result.overview == ""
    assert result.omitted_count == 1


@pytest.mark.asyncio
async def test_direct_overview_cannot_bypass_source_validation(monkeypatch):
    batch = prepare_messages([group_message(-1001)], {}, 10000)
    summarizer = AnthropicSummarizer(SummaryConfig(max_p2_items=0), api_key="key")

    async def fake_request(prompt):
        return DigestResult(overview="unsupported claim", items=[group_item(-1999)])

    monkeypatch.setattr(summarizer, "_request", fake_request)
    result = await summarizer.summarize(batch)
    assert result.overview == ""
    assert result.items == []
    assert result.omitted_count == 1
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_more_than_ten_groups_do_not_lose_coverage_to_source_limit(monkeypatch):
    messages = [group_message(-1000 - index) for index in range(11)]
    batch = prepare_messages(messages, {}, 10000)
    summarizer = AnthropicSummarizer(SummaryConfig(max_p2_items=2), api_key="key")
    template = group_item(-1000)

    async def fake_request(prompt):
        if "<candidates>" in prompt:
            return DigestResult(items=[template])
        chat = prompt_data(prompt, "telegram_records")["source"]["chat_id"]
        return DigestResult(
            items=[template.model_copy(update={"sources": [SourceRef(chat_id=chat, message_id=1)]})]
        )

    monkeypatch.setattr(summarizer, "_request", fake_request)
    result = await summarizer.summarize(batch)
    assert len(result.items) == 2
    assert {s.chat_id for entry in result.items for s in entry.sources} == {
        m.chat_id for m in messages
    }
    assert result.omitted_count == 0
    assert all(len(entry.sources) <= 10 for entry in result.items)
    await summarizer.client.close()


def stream_error(status: int, error_type: str, message: str) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "http://localhost:8317/v1/messages")
    response = httpx.Response(status, request=request)
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    return anthropic.APIStatusError(str(body), response=response, body=body)


def test_transient_error_classification() -> None:
    from tg_digest.summarizer import is_transient_error

    assert is_transient_error(stream_error(200, "overloaded_error", "busy"))
    assert is_transient_error(stream_error(200, "timeout_error", "stream closed"))
    assert is_transient_error(
        stream_error(400, "invalid_request_error", "stream error: stream disconnected")
    )
    assert is_transient_error(stream_error(503, "api_error", "down"))
    assert is_transient_error(
        anthropic.APIConnectionError(request=httpx.Request("POST", "http://localhost"))
    )
    assert not is_transient_error(bad_request())
    assert not is_transient_error(stream_error(404, "not_found_error", "status 404"))
    assert not is_transient_error(ValueError("bad json"))


@pytest.mark.asyncio
async def test_stream_text_retries_transient_errors_then_succeeds(monkeypatch) -> None:
    summarizer = AnthropicSummarizer(SummaryConfig(transient_retry_attempts=2), api_key="key")
    summarizer._retry_base_delay = 0
    calls = 0

    async def flaky(prompt, *, advanced):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise stream_error(200, "service_unavailable_error", "overloaded")
        return '{"items": []}'

    monkeypatch.setattr(summarizer, "_stream_text_once", flaky)
    assert await summarizer._stream_text("p", advanced=False) == '{"items": []}'
    assert calls == 3
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_stream_text_does_not_retry_permanent_errors(monkeypatch) -> None:
    summarizer = AnthropicSummarizer(SummaryConfig(transient_retry_attempts=3), api_key="key")
    summarizer._retry_base_delay = 0
    calls = 0

    async def broken(prompt, *, advanced):
        nonlocal calls
        calls += 1
        raise bad_request()

    monkeypatch.setattr(summarizer, "_stream_text_once", broken)
    with pytest.raises(anthropic.BadRequestError):
        await summarizer._stream_text("p", advanced=False)
    assert calls == 1
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_exhausted_transient_error_does_not_disable_advanced_mode(monkeypatch) -> None:
    summarizer = AnthropicSummarizer(SummaryConfig(transient_retry_attempts=0), api_key="key")

    async def disconnected(prompt, *, advanced):
        raise stream_error(400, "invalid_request_error", "stream disconnected before completion")

    monkeypatch.setattr(summarizer, "_stream_text_once", disconnected)
    with pytest.raises(anthropic.APIStatusError):
        await summarizer._request("p")
    assert summarizer._advanced_api_supported is True
    await summarizer.client.close()


@pytest.mark.asyncio
async def test_extraction_prompt_carries_chat_context_once(monkeypatch) -> None:
    messages = [group_message(-1001, index) for index in (1, 2)]
    messages[1] = messages[1].model_copy(update={"text": "second"})
    group = GroupConfig(name="AI", chat_id=-1001, topics=["API tests"], keywords=["failure"])
    batch = prepare_messages(messages, {-1001: group}, 10000, timezone="Asia/Shanghai")
    summarizer = AnthropicSummarizer(SummaryConfig(), api_key="key")
    prompts = []

    async def fake_request(prompt):
        prompts.append(prompt)
        return DigestResult()

    monkeypatch.setattr(summarizer, "_request", fake_request)
    await summarizer.summarize(batch)
    context = prompt_data(prompts[0], "chat_context")
    assert context == {
        "chat_id": -1001,
        "chat": "Test",
        "priority": "normal",
        "topics": ["API tests"],
        "keywords": ["failure"],
        "timezone": "Asia/Shanghai",
    }
    assert prompts[0].count("API tests") == 1
    await summarizer.client.close()
