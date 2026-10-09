"""Точка входа: python -m sticker_bot"""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.utils.backoff import Backoff, BackoffConfig

from sticker_bot.config import ENV_FILE, Settings
from sticker_bot.handlers import router

logger = logging.getLogger(__name__)


async def run_bot(settings: Settings) -> None:
    dispatcher = Dispatcher(conversion_limiter=asyncio.Semaphore(settings.max_concurrent_conversions))
    dispatcher.include_router(router)
    # Сессия закроется, даже если бота остановят ещё до старта polling
    async with Bot(token=settings.bot_token.get_secret_value()) as bot:
        await wait_for_telegram(bot)
        await dispatcher.start_polling(bot)


async def wait_for_telegram(bot: Bot, backoff_config: BackoffConfig = DEFAULT_BACKOFF_CONFIG) -> None:
    """Повторяет getMe, пока Telegram не ответит.

    Обрывы связи во время polling aiogram переживает сам, а вот getMe перед стартом polling не повторяет:
    если в момент запуска api.telegram.org недоступен, бот падает. bot.me() кеширует ответ,
    поэтому start_polling потом обходится без этого запроса.
    """
    backoff = Backoff(config=backoff_config)
    while True:
        try:
            await bot.me()
            return
        except (TelegramNetworkError, TelegramServerError) as error:
            logger.warning("Telegram is unavailable: %s. Retrying in %.1f s", error, backoff.next_delay)
            await backoff.asleep()


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
