"""Запуск бота: если Telegram недоступен, бот ждёт связи, а не падает."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError
from aiogram.methods import GetMe, TelegramMethod
from aiogram.types import User
from aiogram.utils.backoff import BackoffConfig

from sticker_bot.__main__ import wait_for_telegram

BOT_USER = User(id=123456, is_bot=True, first_name="Sticker bot")
# Паузы между попытками в сотые доли секунды, чтобы тесты не ждали
FAST_BACKOFF = BackoffConfig(min_delay=0.01, max_delay=0.02, factor=2, jitter=0)


class GetMeSession(BaseSession):
    """Отвечает на getMe данными бота, но первые failures запросов падают с ошибкой error."""

    def __init__(self, error: type[TelegramAPIError], failures: int) -> None:
        super().__init__()
        self.error = error
        self.failures = failures
        self.requests = 0

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        assert isinstance(method, GetMe)
        self.requests += 1
        if self.requests <= self.failures:
            raise self.error(method=method, message="simulated failure")
        return BOT_USER

    def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        raise AssertionError("Unexpected download")

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("error", [TelegramNetworkError, TelegramServerError])
def test_waits_until_telegram_responds(error):
    session = GetMeSession(error, failures=3)

    asyncio.run(wait_for_telegram(Bot(token="123456:TEST", session=session), FAST_BACKOFF))

    assert session.requests == 4


def test_invalid_token_is_not_retried():
    session = GetMeSession(TelegramUnauthorizedError, failures=1)

    with pytest.raises(TelegramUnauthorizedError):
        asyncio.run(wait_for_telegram(Bot(token="123456:TEST", session=session), FAST_BACKOFF))

    assert session.requests == 1
