"""Обработчики сообщений бота."""

import asyncio
import logging
from collections.abc import Callable

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, Message, Sticker
from aiogram.utils.chat_action import ChatActionSender

from sticker_bot.converter import tgs_to_gif, webm_to_gif, webp_to_png

logger = logging.getLogger(__name__)

router = Router(name="stickers")

HELP_TEXT = (
    "Пришли мне стикер, и я верну его файлом:\n"
    "• обычный стикер — в формате PNG;\n"
    "• анимированный или видеостикер — в формате GIF."
)
ERROR_TEXT = "Не получилось сконвертировать этот стикер 😔 Попробуй другой."


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(f"Привет! {HELP_TEXT}")


@router.message(F.sticker)
async def handle_sticker(message: Message, bot: Bot, conversion_limiter: asyncio.Semaphore) -> None:
    sticker = message.sticker
    convert, extension = _pick_converter(sticker)
    async with ChatActionSender.upload_document(chat_id=message.chat.id, bot=bot):
        try:
            source = await bot.download(sticker)
            async with conversion_limiter:
                # Конвертация нагружает CPU, поэтому выполняется вне event loop
                result = await asyncio.to_thread(convert, source.read())
        except Exception:
            logger.exception("Failed to convert sticker %s (set %s)", sticker.file_unique_id, sticker.set_name)
            await message.reply(ERROR_TEXT)
            return

        await message.reply_document(
            BufferedInputFile(result, filename=f"sticker_{sticker.file_unique_id}.{extension}"),
            # Без этого Telegram распознает GIF как анимацию и перекодирует его в MP4
            disable_content_type_detection=True,
        )


@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_other(message: Message) -> None:
    await message.answer(HELP_TEXT)


def _pick_converter(sticker: Sticker) -> tuple[Callable[[bytes], bytes], str]:
    """Статичный стикер -> PNG, анимированный (TGS) и видеостикер (WEBM) -> GIF."""
    if sticker.is_animated:
        return tgs_to_gif, "gif"
    if sticker.is_video:
        return webm_to_gif, "gif"
    return webp_to_png, "png"
