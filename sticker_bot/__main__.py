"""Точка входа: python -m sticker_bot"""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher

from sticker_bot.config import ENV_FILE, Settings
from sticker_bot.handlers import router


async def run_bot(settings: Settings) -> None:
    bot = Bot(token=settings.bot_token.get_secret_value())
    dispatcher = Dispatcher(conversion_limiter=asyncio.Semaphore(settings.max_concurrent_conversions))
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    settings = Settings()
    if not settings.has_bot_token:
        sys.exit(f"Укажите BOT_TOKEN в файле {ENV_FILE} — токен выдаёт @BotFather (команда /newbot).")
    try:
        asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        pass  # остановка по Ctrl+C


if __name__ == "__main__":
    main()
