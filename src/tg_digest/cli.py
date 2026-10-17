from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from telethon import utils

from .logging_config import configure_logging
from .settings import load_settings
from .telegram_clients import TelegramClients


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Personal Telegram digest service")
    parser.add_argument("--config", type=Path, help="Path to groups YAML")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("login", help="Create the Telegram user session interactively")
    subparsers.add_parser("list-chats", help="List available Telegram chats")
    subparsers.add_parser("run", help="Run ingestion, scheduler, and bot commands")
    subparsers.add_parser("summary-now", help="Ingest and send one digest, then exit")
    return parser


async def async_main(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    configure_logging(settings)
    clients = TelegramClients(settings)

    if args.command == "login":
        await clients.login_user()
        print(f"Telegram session saved under {settings.data_dir}")
        return

    if args.command == "list-chats":
        await clients.connect_user()
        try:
            print(f"{'CHAT ID':>16}  {'TYPE':<10}  NAME")
            async for dialog in clients.user.iter_dialogs():
                entity = dialog.entity
                kind = "channel" if getattr(entity, "broadcast", False) else "group"
                if getattr(entity, "megagroup", False):
                    kind = "supergroup"
                if getattr(entity, "first_name", None):
                    kind = "user"
                print(f"{utils.get_peer_id(entity):>16}  {kind:<10}  {dialog.name}")
        finally:
            await clients.disconnect()
        return

    from .application import run_application

    await run_application(settings, run_once=args.command == "summary-now")


def main() -> None:
    asyncio.run(async_main(build_parser().parse_args()))


if __name__ == "__main__":
    main()
