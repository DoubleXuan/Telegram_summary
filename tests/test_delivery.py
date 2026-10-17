from types import SimpleNamespace

import pytest

from tg_digest.delivery import TelegramDelivery


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(
        self,
        entity: int,
        message: str,
        *,
        parse_mode: str,
        link_preview: bool,
    ) -> SimpleNamespace:
        self.sent.append((entity, message))
        return SimpleNamespace(id=100 + len(self.sent))


class RecordingDatabase:
    def __init__(self) -> None:
        self.deliveries: list[tuple[str, int]] = []

    async def record_delivery(self, run_id: str, message_id: int) -> None:
        self.deliveries.append((run_id, message_id))


@pytest.mark.asyncio
async def test_delivery_skips_parts_with_recorded_receipts() -> None:
    bot = RecordingBot()
    database = RecordingDatabase()
    delivery = TelegramDelivery(bot, 123, database)  # type: ignore[arg-type]

    delivered = await delivery.send_parts("run1", ["part one", "part two"], [55])

    assert delivered == [55, 101]
    assert bot.sent == [(123, "part two")]
    assert database.deliveries == [("run1", 101)]
