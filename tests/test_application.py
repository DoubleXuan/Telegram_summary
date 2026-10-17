import pytest

from tg_digest.application import run_scheduled_digest
from tg_digest.service import ServiceResult


class FakeIngestion:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def catch_up_now(self) -> int:
        self.events.append("catch_up")
        return 7


class FakeService:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def drain_scheduled(self) -> ServiceResult:
        self.events.append("drain")
        return ServiceResult(status="delivered", message_count=7)


@pytest.mark.asyncio
async def test_scheduled_digest_catches_up_before_draining(caplog) -> None:
    caplog.set_level("INFO")
    events: list[str] = []

    result = await run_scheduled_digest(
        FakeIngestion(events),  # type: ignore[arg-type]
        FakeService(events),  # type: ignore[arg-type]
    )

    assert events == ["catch_up", "drain"]
    assert result.status == "delivered"
    assert result.message_count == 7
    assert "Scheduled catch-up saved 7 new messages" in caplog.text
    assert "Scheduled digest finished: status=delivered messages=7" in caplog.text
