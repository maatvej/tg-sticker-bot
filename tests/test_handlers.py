"""Сквозная проверка бота: апдейт от Telegram -> ответ бота, без обращений к сети."""

import asyncio
import gc
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, GetFile, SendChatAction, SendDocument, SendMessage, TelegramMethod
from aiogram.types import BufferedInputFile, CallbackQuery, Chat, File, Message, Sticker, Update, User

from sticker_bot.converter import OutputFormat
from sticker_bot.flood_control import RetryAfterMiddleware, UploadStatus
from sticker_bot.handlers import ERROR_TEXT, HELP_TEXT, SOURCE_LOST_TEXT, FormatChoice, router

CHAT = Chat(id=1, type="private")
USER = User(id=1, is_bot=False, first_name="Tester")
BOT_USER = User(id=123456, is_bot=True, first_name="Sticker bot")
STICKER_FILE_ID = "sticker-file-id"


class FakeSession(BaseSession):
    """Подменяет HTTP-запросы к Telegram: отдаёт файл стикера и запоминает вызванные методы.

    floods — сколько раз ответить на метод ошибкой 429 (flood control), прежде чем выполнить его.
    """

    def __init__(self, sticker_data: bytes, floods: dict[type[TelegramMethod[Any]], int] | None = None) -> None:
        super().__init__()
        self.sticker_data = sticker_data
        self.floods = dict(floods or {})
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.requests.append(method)
        if self.floods.get(type(method)):
            self.floods[type(method)] -= 1
            raise TelegramRetryAfter(method=method, message="Too Many Requests: retry after 0", retry_after=0)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="file", file_path=f"stickers/{method.file_id}")
        if isinstance(method, (SendChatAction, AnswerCallbackQuery)):
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


def send(
    dispatcher: Dispatcher, update: Update | Message, sticker_data: bytes = b"", session: FakeSession | None = None
) -> list[TelegramMethod[Any]]:
    """Прогоняет апдейт через бота и возвращает ответы бота (без скачивания и статуса «отправляет файл»)."""
    session = session or FakeSession(sticker_data)
    if isinstance(update, Message):
        update = Update(update_id=1, message=update)

    async def feed() -> None:
        bot = Bot(token="123456:TEST", session=session)
        await dispatcher.feed_update(bot, update, **dependencies())

    asyncio.run(feed())
    return [request for request in session.requests if not isinstance(request, (GetFile, SendChatAction))]


def dependencies() -> dict[str, Any]:
    """То, что __main__ передаёт обработчикам через диспетчер."""
    return {"conversion_limiter": asyncio.Semaphore(2), "upload_status": UploadStatus()}


def make_message(message_id: int = 1, **fields: Any) -> Message:
    return Message(message_id=message_id, date=datetime.now(), chat=CHAT, from_user=USER, **fields)


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


def press_button(sticker_message: Message | None, output: OutputFormat) -> Update:
    """Нажатие кнопки под файлом, который бот прислал ответом на sticker_message."""
    bot_reply = Message(
        message_id=2, date=datetime.now(), chat=CHAT, from_user=BOT_USER, reply_to_message=sticker_message
    )
    callback = CallbackQuery(
        id="42", from_user=USER, chat_instance="1", message=bot_reply, data=FormatChoice(output=output).pack()
    )
    return Update(update_id=2, callback_query=callback)


def button_texts(reply: SendDocument) -> list[str]:
    return [button.text for row in reply.reply_markup.inline_keyboard for button in row]


@pytest.mark.parametrize(
    ("sample_name", "is_animated", "is_video", "filename", "signature", "buttons"),
    [
        ("webp_sticker", False, False, "sticker_AgADtest.png", b"\x89PNG\r\n\x1a\n", ["WebP"]),
        ("tgs_sticker", True, False, "sticker_AgADtest.gif", b"GIF89a", ["APNG", "WebP", "WebM"]),
        ("webm_sticker", False, True, "sticker_AgADtest.gif", b"GIF89a", ["APNG", "WebP", "WebM"]),
    ],
)
def test_sticker_is_sent_back_as_file(
    request, dispatcher, sample_name, is_animated, is_video, filename, signature, buttons
):
    sample = request.getfixturevalue(sample_name)
    message = make_message(sticker=make_sticker(is_animated=is_animated, is_video=is_video))

    [reply] = send(dispatcher, message, sample.data)

    assert isinstance(reply, SendDocument)
    assert isinstance(reply.document, BufferedInputFile)
    assert reply.document.filename == filename
    assert reply.document.data.startswith(signature)
    assert reply.disable_content_type_detection is True
    assert reply.reply_parameters.message_id == message.message_id
    assert button_texts(reply) == buttons


@pytest.mark.parametrize(
    ("output", "signature", "buttons"),
    [
        (OutputFormat.APNG, b"\x89PNG\r\n\x1a\n", ["GIF", "WebP", "WebM"]),
        (OutputFormat.WEBP, b"RIFF", ["GIF", "APNG", "WebM"]),
        (OutputFormat.WEBM, b"\x1a\x45\xdf\xa3", ["GIF", "APNG", "WebP"]),
    ],
)
def test_format_button_converts_original_sticker(dispatcher, tgs_sticker, output, signature, buttons):
    sticker_message = make_message(sticker=make_sticker(is_animated=True))

    answer, reply = send(dispatcher, press_button(sticker_message, output), tgs_sticker.data)

    assert isinstance(answer, AnswerCallbackQuery)
    assert not answer.show_alert
    assert isinstance(reply, SendDocument)
    assert reply.document.filename == f"sticker_AgADtest.{output.value}"
    assert reply.document.data.startswith(signature)
    assert reply.reply_parameters.message_id == sticker_message.message_id
    assert button_texts(reply) == buttons


def test_format_button_without_original_sticker_shows_alert(dispatcher):
    [answer] = send(dispatcher, press_button(None, OutputFormat.APNG))

    assert isinstance(answer, AnswerCallbackQuery)
    assert answer.show_alert
    assert answer.text == SOURCE_LOST_TEXT


def test_burst_of_stickers_shows_one_upload_status_per_chat(dispatcher, webp_sticker):
    """Пачка стикеров разом, а Telegram отвечает на статус «отправляет файл» ошибкой 429.

    Статус должен быть один на чат, а не на каждый стикер, его ошибки не должны всплывать
    как «Task exception was never retrieved», и все файлы должны дойти.
    """
    session = FakeSession(webp_sticker.data, floods={SendChatAction: 1000})
    updates = [Update(update_id=i, message=make_message(i, sticker=make_sticker())) for i in range(1, 11)]
    unretrieved_errors: list[dict[str, Any]] = []

    async def feed() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: unretrieved_errors.append(context))
        bot = Bot(token="123456:TEST", session=session)
        shared = dependencies()
        await asyncio.gather(*(dispatcher.feed_update(bot, update, **shared) for update in updates))
        gc.collect()  # о непрочитанном исключении задачи asyncio сообщает, когда задачу удаляет сборщик мусора

    asyncio.run(feed())

    assert sum(isinstance(request, SendChatAction) for request in session.requests) == 1
    assert sum(isinstance(request, SendDocument) for request in session.requests) == 10
    assert unretrieved_errors == []


def test_file_is_sent_again_after_flood_control(dispatcher, webp_sticker):
    session = FakeSession(webp_sticker.data, floods={SendDocument: 1})
    session.middleware(RetryAfterMiddleware())

    replies = send(dispatcher, make_message(sticker=make_sticker()), session=session)

    # Первая попытка получила 429, вторая прошла
    assert len(replies) == 2
    assert all(isinstance(reply, SendDocument) for reply in replies)


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
