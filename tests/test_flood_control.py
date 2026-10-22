"""Защита от flood control: статус «отправляет файл» и повтор запросов после ошибки 429."""

import asyncio
from typing import Any

import pytest
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendDocument, TelegramMethod

from sticker_bot.flood_control import RetryAfterMiddleware, UploadStatus


class StatusRecorder:
    """Вместо бота: запоминает, в какие чаты отправлялся статус."""

    def __init__(self) -> None:
        self.chats: list[int] = []

    async def send_chat_action(self, chat_id: int, action: ChatAction) -> bool:
        self.chats.append(chat_id)
        return True


def test_each_chat_gets_one_status_however_many_conversions():
    async def scenario() -> list[int]:
        bot, upload_status = StatusRecorder(), UploadStatus()

        async def convert(chat_id: int) -> None:
            async with upload_status.shown(bot, chat_id):
                await asyncio.sleep(0.05)

        await asyncio.gather(convert(1), convert(1), convert(1), convert(2))
        return bot.chats

    assert sorted(asyncio.run(scenario())) == [1, 2]


def test_status_is_repeated_until_last_conversion_in_chat_ends():
    async def scenario() -> tuple[int, int]:
        bot, upload_status = StatusRecorder(), UploadStatus(interval=0.02)
        async with upload_status.shown(bot, 1):
            async with upload_status.shown(bot, 1):
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.2)  # одна конвертация закончилась, вторая ещё идёт
        sent = len(bot.chats)
        await asyncio.sleep(0.1)
        return sent, len(bot.chats)

    sent, sent_later = asyncio.run(scenario())
    assert sent >= 5
    assert sent_later == sent


def flood(method: TelegramMethod[Any]) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=method, message="Too Many Requests: retry after 7", retry_after=7)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Паузы, которые middleware попросил выждать; сами паузы пропускаются, чтобы тесты не ждали."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


def test_request_is_retried_after_waiting_as_telegram_asks(sleeps):
    method = SendDocument(chat_id=1, document="file-id")
    attempts = 0

    async def make_request(bot: Any, method: TelegramMethod[Any]) -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise flood(method)
        return "sent"

    assert asyncio.run(RetryAfterMiddleware()(make_request, None, method)) == "sent"
    assert sleeps == [7, 7]


def test_request_gives_up_after_max_retries(sleeps):
    method = SendDocument(chat_id=1, document="file-id")
    attempts = 0

    async def make_request(bot: Any, method: TelegramMethod[Any]) -> str:
        nonlocal attempts
        attempts += 1
        raise flood(method)

    with pytest.raises(TelegramRetryAfter):
        asyncio.run(RetryAfterMiddleware(max_retries=2)(make_request, None, method))
    assert attempts == 3
