"""Конвертация стикеров Telegram.

* статичный стикер (WEBP) -> PNG;
* анимированный стикер (TGS — Lottie JSON, сжатый gzip) -> GIF;
* видеостикер (WEBM, VP9 с альфа-каналом) -> GIF.

Функции синхронные и нагружают CPU, поэтому из асинхронного кода
их нужно вызывать в отдельном потоке.
"""

import functools
import gzip
import io
import logging
import math
import subprocess
import tempfile
import threading
import zlib
from fractions import Fraction
from pathlib import Path

import imageio_ffmpeg
from PIL import Image
from rlottie_python import LottieAnimation

logger = logging.getLogger(__name__)

# Задержка кадра в GIF хранится в сотых долях секунды, а задержки меньше 2/100 с
# браузеры и мессенджеры не соблюдают — анимация начинает заметно тормозить.
# Поэтому более высокую частоту (у TGS обычно 60 fps) снижаем прореживанием кадров.
MAX_GIF_FPS = 50

FFMPEG_TIMEOUT_SECONDS = 60

# Одна палитра на всю анимацию, чтобы цвета не «мерцали» от кадра к кадру. Последний
# цвет палитры зарезервирован под прозрачность: полупрозрачности в GIF нет, поэтому
# пиксели с альфой меньше 128 становятся прозрачными, остальные — непрозрачными.
_GIF_OUTPUT_ARGS = (
    "-filter_complex",
    "[0:v]split[a][b];"
    "[a]palettegen=reserve_transparent=1[palette];"
    "[b][palette]paletteuse=alpha_threshold=128",
    "-loop", "0",
    "-f", "gif",
    "pipe:1",
)

# rlottie-python не потокобезопасен: при каждом вызове он заново присваивает argtypes
# общим ctypes-функциям библиотеки, и вызовы из разных потоков мешают друг другу.
_rlottie_lock = threading.Lock()


class ConversionError(Exception):
    """Стикер не удалось сконвертировать."""


def webp_to_png(data: bytes) -> bytes:
    """Статичный стикер (WEBP) -> PNG с сохранением прозрачности."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            output = io.BytesIO()
            image.save(output, format="PNG")
    except OSError as error:  # в том числе PIL.UnidentifiedImageError
        raise ConversionError("Cannot decode the static sticker") from error
    return output.getvalue()


def tgs_to_gif(data: bytes) -> bytes:
    """Анимированный стикер (TGS) -> GIF."""
    try:
        lottie_json = gzip.decompress(data).decode("utf-8")
    except (OSError, EOFError, zlib.error, UnicodeDecodeError) as error:
        raise ConversionError("TGS is not a gzip-compressed Lottie JSON") from error

    with tempfile.TemporaryDirectory(prefix="tg-sticker-") as tmp_dir:
        # Кадры складываем на диск, а не в память: 3 секунды анимации 512x512 — это ~100 МБ
        frames_path = Path(tmp_dir) / "frames.rgba"
        with _rlottie_lock:
            width, height, fps = _render_lottie(lottie_json, frames_path)
        return _run_ffmpeg(
            "-f", "rawvideo",
            "-pix_fmt", "rgba",
            "-video_size", f"{width}x{height}",
            "-framerate", str(fps),
            "-i", str(frames_path),
            *_GIF_OUTPUT_ARGS,
        )


def webm_to_gif(data: bytes) -> bytes:
    """Видеостикер (WEBM) -> GIF."""
    # Встроенный в ffmpeg декодер VP9 отбрасывает альфа-канал (фон стал бы чёрным),
    # поэтому явно выбираем декодер libvpx-vp9.
    try:
        return _run_ffmpeg("-c:v", "libvpx-vp9", "-i", "pipe:0", *_GIF_OUTPUT_ARGS, stdin_data=data)
    except ConversionError as error:
        # Например, ролик закодирован в VP8, а не в VP9: пусть ffmpeg сам выберет декодер
        logger.warning("libvpx-vp9 failed, retrying with the default decoder: %s", error)
        return _run_ffmpeg("-i", "pipe:0", *_GIF_OUTPUT_ARGS, stdin_data=data)


def _render_lottie(lottie_json: str, frames_path: Path) -> tuple[int, int, Fraction]:
    """Рендерит кадры анимации в файл (сырые RGBA подряд). Возвращает ширину, высоту и fps."""
    with LottieAnimation.from_data(lottie_json) as animation:
        if not animation.animation_p:
            raise ConversionError("rlottie cannot parse the animation")
        width, height = animation.lottie_animation_get_size()
        fps = animation.lottie_animation_get_framerate()
        if width <= 0 or height <= 0 or fps <= 0:
            raise ConversionError(f"Invalid animation: {width}x{height} at {fps} fps")

        # rlottie считает кадр op включительно, хотя он уже за концом анимации, и на стыке
        # цикла получился бы лишний кадр. Поэтому число кадров считаем по длительности.
        frame_count = max(1, round(animation.lottie_animation_get_duration() * fps))
        step = math.ceil(fps / MAX_GIF_FPS)
        with frames_path.open("wb") as frames:
            for frame_num in range(0, frame_count, step):
                buffer = animation.lottie_animation_render(frame_num=frame_num, width=width, height=height)
                # rlottie отдаёт BGRA с premultiplied-альфой. Режим "BGRa" переводит его
                # в обычный RGBA, иначе полупрозрачные края стикера получатся тёмными.
                frame = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRa", 0, 1)
                frames.write(frame.tobytes())
    return width, height, Fraction(fps).limit_denominator(1001) / step


def _run_ffmpeg(*args: str, stdin_data: bytes | None = None) -> bytes:
    """Запускает ffmpeg и возвращает то, что он записал в stdout."""
    command = [_ffmpeg_executable(), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    try:
        result = subprocess.run(
            command,
            input=stdin_data,
            stdin=subprocess.DEVNULL if stdin_data is None else None,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            # На Windows не открывать окно консоли, если бот запущен без неё (например, службой)
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise ConversionError(f"ffmpeg did not finish in {FFMPEG_TIMEOUT_SECONDS} s") from error
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode(errors="replace").strip()
        raise ConversionError(f"ffmpeg failed with exit code {result.returncode}: {stderr}")
    return result.stdout


@functools.cache
def _ffmpeg_executable() -> str:
    # ffmpeg ставится вместе с пакетом imageio-ffmpeg;
    # свой бинарник можно указать в переменной окружения IMAGEIO_FFMPEG_EXE.
    return imageio_ffmpeg.get_ffmpeg_exe()
