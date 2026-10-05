"""Сквозная проверка бота: апдейт от Telegram -> ответ бота, без обращений к сети."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile, SendChatAction, SendDocument, SendMessage, TelegramMethod
from aiogram.types import BufferedInputFile, Chat, File, Message, Sticker, Update, User

from sticker_bot.handlers import ERROR_TEXT, HELP_TEXT, router

CHAT = Chat(id=1, type="private")
USER = User(id=1, is_bot=False, first_name="Tester")
STICKER_FILE_ID = "sticker-file-id"


class FakeSession(BaseSession):
    """Подменяет HTTP-запросы к Telegram: отдаёт файл стикера и запоминает вызванные методы."""

    def __init__(self, sticker_data: bytes) -> None:
        super().__init__()
        self.sticker_data = sticker_data
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.requests.append(method)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="file", file_path=f"stickers/{method.file_id}")
        if isinstance(method, SendChatAction):
            return True
        if isinstance(method, (SendMessage, SendDocument)):
            return Message(message_id=2, date=datetime.now(), chat=CHAT)
        raise AssertionError(f"Unexpected request: {type(method).__name__}")

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        assert url.endswith(f"/stickers/{STICKER_FILE_ID}")
        yield self.sticker_data

    async def close(self) -> None:
        pass


@pytest.fixture(scope="module")
def dispatcher() -> Dispatcher:
    # Роутер подключается только к одному диспетчеру, поэтому диспетчер общий на модуль
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


def send(dispatcher: Dispatcher, message: Message, sticker_data: bytes = b"") -> list[TelegramMethod[Any]]:
    """Прогоняет сообщение через бота и возвращает ответы бота (без статуса «отправляет файл»)."""
    session = FakeSession(sticker_data)

    async def feed() -> None:
        bot = Bot(token="123456:TEST", session=session)
        update = Update(update_id=1, message=message)
        await dispatcher.feed_update(bot, update, conversion_limiter=asyncio.Semaphore(1))

    asyncio.run(feed())
    return [request for request in session.requests if not isinstance(request, (GetFile, SendChatAction))]


def make_message(**fields: Any) -> Message:
    return Message(message_id=1, date=datetime.now(), chat=CHAT, from_user=USER, **fields)


def make_sticker(*, is_animated: bool = False, is_video: bool = False) -> Sticker:
    return Sticker(
        file_id=STICKER_FILE_ID,
        file_unique_id="AgADtest",
        type="regular",
        width=512,
        height=512,
        is_animated=is_animated,
        is_video=is_video,
    )


@pytest.mark.parametrize(
    ("sample_name", "is_animated", "is_video", "filename", "signature"),
    [
        ("webp_sticker", False, False, "sticker_AgADtest.png", b"\x89PNG\r\n\x1a\n"),
        ("tgs_sticker", True, False, "sticker_AgADtest.gif", b"GIF89a"),
        ("webm_sticker", False, True, "sticker_AgADtest.gif", b"GIF89a"),
    ],
)
def test_sticker_is_sent_back_as_file(request, dispatcher, sample_name, is_animated, is_video, filename, signature):
    sample = request.getfixturevalue(sample_name)
    message = make_message(sticker=make_sticker(is_animated=is_animated, is_video=is_video))

    [reply] = send(dispatcher, message, sample.data)

    assert isinstance(reply, SendDocument)
    assert isinstance(reply.document, BufferedInputFile)
    assert reply.document.filename == filename
    assert reply.document.data.startswith(signature)
    assert reply.disable_content_type_detection is True
    assert reply.reply_parameters.message_id == message.message_id


def test_broken_sticker_gets_error_message(dispatcher):
    message = make_message(sticker=make_sticker(is_animated=True))

    [reply] = send(dispatcher, message, b"broken")

    assert isinstance(reply, SendMessage)
    assert reply.text == ERROR_TEXT


def test_start_command_greets(dispatcher):
    [reply] = send(dispatcher, make_message(text="/start"))

    assert isinstance(reply, SendMessage)
    assert reply.text.startswith("Привет!")


def test_other_message_gets_help(dispatcher):
    [reply] = send(dispatcher, make_message(text="как дела?"))

    assert isinstance(reply, SendMessage)
    assert reply.text == HELP_TEXT
