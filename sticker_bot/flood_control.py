"""Защита от flood control Telegram.

На слишком частые запросы в один чат Telegram отвечает ошибкой 429 (Too Many Requests) и сообщает,
сколько секунд подождать. Без этой защиты пачка стикеров, присланная разом, заваливала бы чат
статусами «отправляет файл», а ответы с файлами могли бы теряться.
"""

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncIterator

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType

logger = logging.getLogger(__name__)


class RetryAfterMiddleware(BaseRequestMiddleware):
    """Если Telegram просит подождать, ждёт сколько сказано и повторяет запрос."""

    def __init__(self, max_retries: int = 3) -> None:
        self.max_retries = max_retries

    async def __call__(
        self, make_request: NextRequestMiddlewareType[TelegramType], bot: Bot, method: TelegramMethod[TelegramType]
    ) -> Response[TelegramType]:
        retries = 0
        while True:
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as error:
                if retries == self.max_retries:
                    raise
                retries += 1
                logger.warning("Flood control on %s, retrying in %s s", type(method).__name__, error.retry_after)
                await asyncio.sleep(error.retry_after)


@dataclasses.dataclass
class _ChatStatus:
    task: asyncio.Task[None]
    conversions: int = 0


class UploadStatus:
    """Статус «отправляет файл…» в чатах, где идут конвертации.

    Telegram показывает статус 5 секунд, поэтому его приходится повторять. На чат работает один
    фоновый цикл, сколько бы стикеров там ни конвертировалось. ChatActionSender из aiogram запускает
    отдельный цикл на каждый стикер, и пачка стикеров упирается в flood control, а упавший цикл
    оставляет исключение, которое никто не забирает («Task exception was never retrieved»).
    """

    def __init__(self, interval: float = 5.0) -> None:
        self._interval = interval
        self._chats: dict[int, _ChatStatus] = {}

    @contextlib.asynccontextmanager
    async def shown(self, bot: Bot, chat_id: int) -> AsyncIterator[None]:
        """Показывает статус в чате, пока выполняется тело блока with."""
        status = self._chats.get(chat_id)
        if status is None:
            status = self._chats[chat_id] = _ChatStatus(asyncio.create_task(self._repeat(bot, chat_id)))
        status.conversions += 1
        try:
            yield
        finally:
            status.conversions -= 1
            if not status.conversions:
                del self._chats[chat_id]
                status.task.cancel()

    async def _repeat(self, bot: Bot, chat_id: int) -> None:
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
            except Exception as error:  # статус — лишь подсказка, его сбои не должны мешать конвертации
                logger.debug("Cannot show upload status in chat %s: %s", chat_id, error)
            await asyncio.sleep(self._interval)
