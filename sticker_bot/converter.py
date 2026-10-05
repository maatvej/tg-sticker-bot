"""Конвертация стикеров Telegram.

Стикеры бывают трёх видов: статичные (WEBP), анимированные (TGS — Lottie JSON, сжатый gzip)
и видеостикеры (WEBM, VP9 с альфа-каналом). Статичный стикер конвертируется в PNG или WebP,
анимированный и видеостикер — в GIF, APNG, WebP или WebM.

Функции синхронные и нагружают CPU, поэтому из асинхронного кода
их нужно вызывать в отдельном потоке.
"""

import contextlib
import dataclasses
import functools
import gzip
import io
import logging
import subprocess
import tempfile
import threading
import zlib
from collections.abc import Callable, Iterator
from enum import Enum, StrEnum
from fractions import Fraction
from pathlib import Path

import imageio_ffmpeg
from PIL import Image
from rlottie_python import LottieAnimation

logger = logging.getLogger(__name__)


class StickerKind(Enum):
    STATIC = "static"  # WEBP
    ANIMATED = "animated"  # TGS — Lottie JSON, сжатый gzip
    VIDEO = "video"  # WEBM — VP9 с альфа-каналом


class OutputFormat(StrEnum):
    """Формат результата; значение — расширение файла."""

    PNG = "png"
    GIF = "gif"
    APNG = "apng"
    WEBP = "webp"
    WEBM = "webm"


# Во что можно сконвертировать стикер каждого вида; первый формат — формат по умолчанию
OUTPUT_FORMATS: dict[StickerKind, tuple[OutputFormat, ...]] = {
    StickerKind.STATIC: (OutputFormat.PNG, OutputFormat.WEBP),
    StickerKind.ANIMATED: (OutputFormat.GIF, OutputFormat.APNG, OutputFormat.WEBP, OutputFormat.WEBM),
    StickerKind.VIDEO: (OutputFormat.GIF, OutputFormat.APNG, OutputFormat.WEBP, OutputFormat.WEBM),
}

# Задержка кадра в GIF задаётся в сотых долях секунды, поэтому 60 fps (16,7 мс на кадр)
# можно записать только чередованием задержек 20 и 10 мс. Но браузеры (Chrome, Firefox,
# Safari) любую задержку до 10 мс включительно растягивают до 100 мс, и такой GIF играл бы
# в 2,8 раза медленнее. Самая короткая задержка, которую соблюдают все, — 20 мс, то есть
# 50 fps. Анимации чаще 50 fps (у TGS обычно 60) пересэмплируются в 50 fps, остальные не меняются.
# В APNG, WebP и WebM таких ограничений нет, туда кадры попадают все.
MAX_GIF_FPS = 50

FFMPEG_TIMEOUT_SECONDS = 60

# Одна палитра на всю анимацию и упорядоченный дизеринг (bayer): одинаковые пиксели соседних
# кадров кодируются одинаково, и неподвижные части стикера не мерцают. С диффузионным дизерингом
# (по умолчанию в ffmpeg) и с палитрой на каждый кадр на реальных стикерах мерцало до 13% и до 36%
# неподвижных пикселей соответственно. bayer_scale=5 даёт самый незаметный узор дизеринга.
# Последний цвет палитры зарезервирован под прозрачность: полупрозрачности в GIF нет, поэтому
# пиксели с альфой меньше 128 становятся прозрачными, остальные — непрозрачными.
_GIF_FILTER = (
    f"[0:v]fps=fps='min(source_fps,{MAX_GIF_FPS})',split[a][b];"
    "[a]palettegen=reserve_transparent=1[palette];"
    "[b][palette]paletteuse=dither=bayer:bayer_scale=5:alpha_threshold=128"
)

# rlottie-python не потокобезопасен: при каждом вызове он заново присваивает argtypes
# общим ctypes-функциям библиотеки, и вызовы из разных потоков мешают друг другу.
_rlottie_lock = threading.Lock()


class ConversionError(Exception):
    """Стикер не удалось сконвертировать."""


@dataclasses.dataclass(frozen=True)
class _Frames:
    """Кадры анимации, записанные в файл подряд как сырые RGBA."""

    path: Path
    width: int
    height: int
    fps: Fraction
    start: int = 0  # с какого кадра читать при переборе

    @property
    def ffmpeg_input(self) -> tuple[str, ...]:
        return (
            "-f", "rawvideo",
            "-pix_fmt", "rgba",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", str(self.fps),
            "-i", str(self.path),
        )

    def __iter__(self) -> Iterator[Image.Image]:
        # Каждый перебор заново читает файл: Pillow при записи APNG проходит по кадрам дважды.
        # Файл открывается на каждый кадр: если кодирование упадёт посреди перебора, недочитанный
        # генератор не будет держать файл открытым, и Windows даст удалить временную папку.
        for index in range(self.start, self._count):
            with self.path.open("rb") as file:
                file.seek(index * self._frame_size)
                frame = file.read(self._frame_size)
            yield Image.frombytes("RGBA", (self.width, self.height), frame)

    def durations_ms(self) -> list[int]:
        """Длительности всех кадров в миллисекундах.

        Округляются моменты начала кадров, а не длительности, поэтому при 60 fps
        длительности чередуются (17, 16, 17 мс), а их сумма в точности равна длине анимации.
        """
        return [round((i + 1) * 1000 / self.fps) - round(i * 1000 / self.fps) for i in range(self._count)]

    @property
    def _frame_size(self) -> int:
        return self.width * self.height * 4

    @property
    def _count(self) -> int:
        return self.path.stat().st_size // self._frame_size


def convert_sticker(data: bytes, kind: StickerKind, output: OutputFormat) -> bytes:
    """Конвертирует стикер вида kind в формат output."""
    if output not in OUTPUT_FORMATS[kind]:
        raise ValueError(f"Cannot convert a {kind.value} sticker to {output.value}")
    if kind is StickerKind.STATIC:
        return _convert_static(data, output)
    if kind is StickerKind.VIDEO and output is OutputFormat.WEBM:
        return data  # видеостикер и так в WebM, перекодирование только ухудшило бы качество

    with tempfile.TemporaryDirectory(prefix="tg-sticker-") as tmp:
        tmp_dir = Path(tmp)
        frames = _render_tgs(data, tmp_dir) if kind is StickerKind.ANIMATED else _decode_webm(data, tmp_dir)
        output_path = tmp_dir / f"sticker.{output.value}"
        _ENCODERS[output](frames, output_path)
        if not output_path.is_file() or not output_path.stat().st_size:
            raise ConversionError(f"Encoder produced no {output.value} file")
        return output_path.read_bytes()


def _convert_static(data: bytes, output: OutputFormat) -> bytes:
    """Статичный стикер -> PNG или WebP с сохранением прозрачности."""
    # Статичные стикеры Telegram и так хранятся в WebP: отдаём исходный файл без перекодирования
    if output is OutputFormat.WEBP and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data
    try:
        with Image.open(io.BytesIO(data)) as image:
            result = io.BytesIO()
            if output is OutputFormat.PNG:
                image.save(result, format="PNG")
            else:
                image.save(result, format="WEBP", lossless=True)
    except OSError as error:  # в том числе PIL.UnidentifiedImageError
        raise ConversionError("Cannot decode the static sticker") from error
    return result.getvalue()


def _render_tgs(data: bytes, tmp_dir: Path) -> _Frames:
    """Анимированный стикер: рендерит все кадры Lottie-анимации в файл."""
    try:
        lottie_json = gzip.decompress(data).decode("utf-8")
    except (OSError, EOFError, zlib.error, UnicodeDecodeError) as error:
        raise ConversionError("TGS is not a gzip-compressed Lottie JSON") from error

    # Кадры складываем на диск, а не в память: 3 секунды анимации 512x512 при 60 fps — это ~190 МБ
    path = tmp_dir / "frames.rgba"
    with _rlottie_lock, LottieAnimation.from_data(lottie_json) as animation:
        if not animation.animation_p:
            raise ConversionError("rlottie cannot parse the animation")
        width, height = animation.lottie_animation_get_size()
        fps = animation.lottie_animation_get_framerate()
        if width <= 0 or height <= 0 or fps <= 0:
            raise ConversionError(f"Invalid animation: {width}x{height} at {fps} fps")

        # rlottie считает кадр op включительно, хотя он уже за концом анимации, и на стыке
        # цикла получился бы лишний кадр. Поэтому число кадров считаем по длительности.
        frame_count = max(1, round(animation.lottie_animation_get_duration() * fps))
        with path.open("wb") as frames:
            for frame_num in range(frame_count):
                buffer = animation.lottie_animation_render(frame_num=frame_num, width=width, height=height)
                # rlottie отдаёт BGRA с premultiplied-альфой. Режим "BGRa" переводит его
                # в обычный RGBA, иначе полупрозрачные края стикера получатся тёмными.
                frame = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRa", 0, 1)
                frames.write(frame.tobytes())
    return _Frames(path, width, height, Fraction(fps).limit_denominator(1001))


def _decode_webm(data: bytes, tmp_dir: Path) -> _Frames:
    """Видеостикер: декодирует все кадры в файл."""
    source = tmp_dir / "source.webm"
    source.write_bytes(data)
    # Встроенный в ffmpeg декодер VP9 отбрасывает альфа-канал (фон стал бы чёрным),
    # поэтому явно выбираем декодер libvpx-vp9. Ошибки ffmpeg imageio-ffmpeg сообщает
    # через OSError (не прочитался заголовок) и RuntimeError (оборвался поток кадров).
    try:
        return _decode_video(source, tmp_dir, decoder=("-c:v", "libvpx-vp9"))
    except (OSError, RuntimeError) as error:
        # Например, ролик закодирован в VP8, а не в VP9: пусть ffmpeg сам выберет декодер
        logger.warning("libvpx-vp9 failed, retrying with the default decoder: %s", error)
    try:
        return _decode_video(source, tmp_dir, decoder=())
    except (OSError, RuntimeError) as error:
        raise ConversionError("Cannot decode the video sticker") from error


def _decode_video(source: Path, tmp_dir: Path, decoder: tuple[str, ...]) -> _Frames:
    path = tmp_dir / "frames.rgba"
    reader = imageio_ffmpeg.read_frames(str(source), pix_fmt="rgba", bits_per_pixel=32, input_params=list(decoder))
    with contextlib.closing(reader), path.open("wb") as frames:
        meta = next(reader)
        for frame in reader:
            frames.write(frame)
    width, height = meta["size"]
    if meta["fps"] <= 0 or not path.stat().st_size:
        raise ConversionError("Cannot decode the video sticker")
    return _Frames(path, width, height, Fraction(meta["fps"]).limit_denominator(1001))


def _encode_gif(frames: _Frames, output_path: Path) -> None:
    _run_ffmpeg(*frames.ffmpeg_input, "-filter_complex", _GIF_FILTER, "-loop", "0", "-f", "gif", str(output_path))


def _encode_apng(frames: _Frames, output_path: Path) -> None:
    # APNG сохраняет Pillow: он записывает только изменившиеся области кадров и на реальных
    # стикерах работает в 8 раз быстрее ffmpeg, а файл получается меньше
    first = next(iter(frames))
    rest = dataclasses.replace(frames, start=1)
    first.save(output_path, format="PNG", save_all=True, append_images=rest, duration=frames.durations_ms(), loop=0)


def _encode_webp(frames: _Frames, output_path: Path) -> None:
    # Сжатие с потерями (quality 90) на глаз не отличить от исходника, а файл втрое легче, чем
    # без потерь. compression_level 0 кодирует в 4 раза быстрее, чем 4, при почти том же размере.
    _run_ffmpeg(
        *frames.ffmpeg_input,
        "-c:v", "libwebp_anim",
        "-pix_fmt", "bgra",
        "-quality", "90",
        "-compression_level", "0",
        "-loop", "0",
        "-f", "webp",
        str(output_path),
    )
    _make_webp_background_transparent(output_path)


def _make_webp_background_transparent(path: Path) -> None:
    """Меняет цвет фона в заголовке анимированного WebP на прозрачный.

    ffmpeg записывает туда белый цвет, и просмотрщики, которые его учитывают,
    показывают стикер на белом фоне.
    """
    data = bytearray(path.read_bytes())
    offset = 12  # после заголовка RIFF: "RIFF", размер файла, "WEBP"
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        if data[offset : offset + 4] == b"ANIM":
            data[offset + 8 : offset + 12] = bytes(4)  # цвет фона, BGRA
            path.write_bytes(data)
            return
        offset += 8 + size + size % 2  # чанки выровнены по чётной границе


def _encode_webm(frames: _Frames, output_path: Path) -> None:
    # VP9 с альфа-каналом — тот же формат, что у видеостикеров Telegram. Альфа-канал libvpx
    # поддерживает только вместе с цветом в половинном разрешении (yuva420p) и несовместим
    # с alt-ref кадрами, а без них предпросмотр кадров (lag-in-frames) лишь занимает память.
    # По умолчанию ffmpeg запускает столько потоков, сколько ядер: на 16 ядрах это 360 МБ памяти
    # без выигрыша в скорости, а 4 потока кодируют так же быстро и укладываются в 150 МБ.
    _run_ffmpeg(
        *frames.ffmpeg_input,
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0",
        "-lag-in-frames", "0",
        "-crf", "20",
        "-b:v", "0",
        "-deadline", "good",
        "-cpu-used", "2",
        "-row-mt", "1",
        "-threads", "4",
        "-f", "webm",
        str(output_path),
    )


_ENCODERS: dict[OutputFormat, Callable[[_Frames, Path], None]] = {
    OutputFormat.GIF: _encode_gif,
    OutputFormat.APNG: _encode_apng,
    OutputFormat.WEBP: _encode_webp,
    OutputFormat.WEBM: _encode_webm,
}


def _run_ffmpeg(*args: str) -> None:
    command = [_ffmpeg_executable(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            # На Windows не открывать окно консоли, если бот запущен без неё (например, службой)
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise ConversionError(f"ffmpeg did not finish in {FFMPEG_TIMEOUT_SECONDS} s") from error
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise ConversionError(f"ffmpeg failed with exit code {result.returncode}: {stderr}")


@functools.cache
def _ffmpeg_executable() -> str:
    # ffmpeg ставится вместе с пакетом imageio-ffmpeg;
    # свой бинарник можно указать в переменной окружения IMAGEIO_FFMPEG_EXE.
    return imageio_ffmpeg.get_ffmpeg_exe()
