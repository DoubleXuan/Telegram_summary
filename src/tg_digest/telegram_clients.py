from __future__ import annotations

from telethon import TelegramClient

from .settings import AppSettings


class TelegramClients:
    def __init__(self, settings: AppSettings) -> None:
        env = settings.env
        self.user = TelegramClient(
            str(settings.user_session_path), env.telegram_api_id, env.telegram_api_hash
        )
        self.bot = TelegramClient(
            str(settings.bot_session_path), env.telegram_api_id, env.telegram_api_hash
        )
        self.settings = settings

    async def login_user(self) -> None:
        await self.user.start(phone=self.settings.env.telegram_phone)
        await self.user.disconnect()

    async def connect_user(self) -> None:
        await self.user.connect()
        if not await self.user.is_user_authorized():
            await self.user.disconnect()
            raise RuntimeError("Telegram user session is not authorized; run `tg-digest login`")

    async def connect_bot(self) -> None:
        await self.bot.start(bot_token=self.settings.env.telegram_bot_token)

    async def start(self) -> None:
        await self.connect_user()
        await self.connect_bot()

    async def disconnect(self) -> None:
        await self.bot.disconnect()
        await self.user.disconnect()
