"""Обработчики сообщений бота."""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message, Sticker
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_bot.converter import OUTPUT_FORMATS, OutputFormat, StickerKind, convert_sticker

logger = logging.getLogger(__name__)

router = Router(name="stickers")

HELP_TEXT = (
    "Пришли мне стикер, и я верну его файлом:\n"
    "• обычный стикер — в формате PNG;\n"
    "• анимированный или видеостикер — в формате GIF.\n\n"
    "Под файлом будут кнопки, чтобы получить тот же стикер в другом формате. "
    "APNG, WebP и WebM, в отличие от GIF, сохраняют полупрозрачность и все кадры анимации."
)
ERROR_TEXT = "Не получилось сконвертировать этот стикер 😔 Попробуй другой."
SOURCE_LOST_TEXT = "Не нашёл исходный стикер — пришли его ещё раз."

FORMAT_LABELS = {
    OutputFormat.PNG: "PNG",
    OutputFormat.GIF: "GIF",
    OutputFormat.APNG: "APNG",
    OutputFormat.WEBP: "WebP",
    OutputFormat.WEBM: "WebM",
}


class FormatChoice(CallbackData, prefix="format"):
    output: OutputFormat


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(f"Привет! {HELP_TEXT}")


@router.message(F.sticker)
async def handle_sticker(message: Message, bot: Bot, conversion_limiter: asyncio.Semaphore) -> None:
    kind = _sticker_kind(message.sticker)
    await _reply_with_converted(message, OUTPUT_FORMATS[kind][0], bot, conversion_limiter)


@router.callback_query(FormatChoice.filter())
async def handle_format_choice(
    callback: CallbackQuery, callback_data: FormatChoice, bot: Bot, conversion_limiter: asyncio.Semaphore
) -> None:
    # Кнопки висят под файлом, который бот прислал ответом на сообщение со стикером
    source = callback.message.reply_to_message if isinstance(callback.message, Message) else None
    if source is None or source.sticker is None:
        await callback.answer(SOURCE_LOST_TEXT, show_alert=True)
        return
    await callback.answer(f"Конвертирую в {FORMAT_LABELS[callback_data.output]}…")
    await _reply_with_converted(source, callback_data.output, bot, conversion_limiter)


@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_other(message: Message) -> None:
    await message.answer(HELP_TEXT)


async def _reply_with_converted(
    message: Message, output: OutputFormat, bot: Bot, conversion_limiter: asyncio.Semaphore
) -> None:
    """Конвертирует стикер из сообщения и присылает файл ответом на это сообщение."""
    sticker = message.sticker
    kind = _sticker_kind(sticker)
    async with ChatActionSender.upload_document(chat_id=message.chat.id, bot=bot):
        try:
            source = await bot.download(sticker)
            async with conversion_limiter:
                # Конвертация нагружает CPU, поэтому выполняется вне event loop
                result = await asyncio.to_thread(convert_sticker, source.read(), kind, output)
        except Exception:
            logger.exception(
                "Failed to convert sticker %s (set %s) to %s", sticker.file_unique_id, sticker.set_name, output
            )
            await message.reply(ERROR_TEXT)
            return

        await message.reply_document(
            BufferedInputFile(result, filename=f"sticker_{sticker.file_unique_id}.{output.value}"),
            # Без этого Telegram распознает GIF как анимацию и перекодирует его в MP4
            disable_content_type_detection=True,
            reply_markup=_other_formats_keyboard(kind, output),
        )


def _other_formats_keyboard(kind: StickerKind, sent: OutputFormat) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for output in OUTPUT_FORMATS[kind]:
        if output is not sent:
            builder.button(text=FORMAT_LABELS[output], callback_data=FormatChoice(output=output))
    return builder.as_markup()


def _sticker_kind(sticker: Sticker) -> StickerKind:
    if sticker.is_animated:
        return StickerKind.ANIMATED
    if sticker.is_video:
        return StickerKind.VIDEO
    return StickerKind.STATIC
