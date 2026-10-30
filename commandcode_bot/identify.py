"""Identify a private Telegram sender before configuring the allowlist."""

import asyncio
import getpass
import os

from telegram import Bot


async def identify() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        token = getpass.getpass("Paste the BotFather token (input is hidden): ").strip()
    if not token:
        raise SystemExit("No token supplied.")
    async with Bot(token) as bot:
        updates = await bot.get_updates(timeout=5, allowed_updates=["message"])
    senders = {
        (update.message.from_user.id, update.message.from_user.full_name)
        for update in updates
        if update.message and update.message.chat.type == "private" and update.message.from_user
    }
    if not senders:
        print("No private messages found. Send the bot a message, then run this again before starting the service.")
        return
    for user_id, name in sorted(senders):
        print(f"TELEGRAM_USER_ID={user_id}  ({name})")
    print("Use only your own numeric ID in .env. Keep the bot token private.")


if __name__ == "__main__":
    asyncio.run(identify())

