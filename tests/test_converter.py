import gzip
import io
import tempfile
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image, ImageSequence
from rlottie_python import LottieAnimation

from sticker_bot import converter
from sticker_bot.converter import (
    MAX_GIF_FPS,
    OUTPUT_FORMATS,
    ConversionError,
    OutputFormat,
    StickerKind,
    convert_sticker,
)

ANIMATED_SAMPLES = ["tgs_sticker", "tgs_sticker_30fps", "webm_sticker", "webm_sticker_60fps"]
ANIMATED_FORMATS = [OutputFormat.GIF, OutputFormat.APNG, OutputFormat.WEBP, OutputFormat.WEBM]
PILLOW_FORMAT_NAMES = {OutputFormat.GIF: "GIF", OutputFormat.APNG: "PNG", OutputFormat.WEBP: "WEBP"}


def decode(data: bytes, output: OutputFormat) -> tuple[list[Image.Image], float]:
    """Кадры анимации в том виде, в каком их покажет просмотрщик, и её длительность в мс."""
    if output is OutputFormat.WEBM:
        return decode_webm(data)
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == PILLOW_FORMAT_NAMES[output]
        frames, duration = [], 0.0
        for frame in ImageSequence.Iterator(image):
            frames.append(frame.convert("RGBA"))
            duration += frame.info["duration"]
    return frames, duration


def decode_webm(data: bytes) -> tuple[list[Image.Image], float]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "video.webm"
        path.write_bytes(data)
        # Декодер libvpx-vp9 — единственный в ffmpeg, который читает альфа-канал VP9
        reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgba", bits_per_pixel=32, input_params=["-c:v", "libvpx-vp9"])
        meta = next(reader)
        frames = [Image.frombytes("RGBA", meta["size"], frame) for frame in reader]
    return frames, meta["duration"] * 1000


def is_red(pixel: tuple[int, int, int, int]) -> bool:
    red, green, blue, alpha = pixel
    return alpha > 240 and red > 200 and green < 60 and blue < 60


def is_transparent(pixel: tuple[int, int, int, int]) -> bool:
    return pixel[3] < 16


def test_static_sticker_becomes_png_with_transparency(webp_sticker):
    with Image.open(io.BytesIO(convert_sticker(webp_sticker.data, StickerKind.STATIC, OutputFormat.PNG))) as image:
        assert image.format == "PNG"
        assert image.size == (512, 512)
        image = image.convert("RGBA")
        assert image.getpixel((5, 5))[3] == 0
        assert image.getpixel((256, 256)) == (255, 0, 0, 255)


def test_static_sticker_to_webp_returns_original(webp_sticker):
    assert convert_sticker(webp_sticker.data, StickerKind.STATIC, OutputFormat.WEBP) == webp_sticker.data


def test_static_non_webp_sticker_is_reencoded_to_lossless_webp():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    image.paste((255, 0, 0, 128), (16, 16, 48, 48))
    png = io.BytesIO()
    image.save(png, format="PNG")

    with Image.open(io.BytesIO(convert_sticker(png.getvalue(), StickerKind.STATIC, OutputFormat.WEBP))) as webp:
        assert webp.format == "WEBP"
        webp = webp.convert("RGBA")
        assert webp.getpixel((2, 2))[3] == 0
        assert webp.getpixel((32, 32)) == (255, 0, 0, 128)


@pytest.mark.parametrize("output", ANIMATED_FORMATS)
@pytest.mark.parametrize("sample_name", ANIMATED_SAMPLES)
def test_animated_sticker_conversion(request, sample_name, output):
    sample = request.getfixturevalue(sample_name)
    frames, duration_ms = decode(convert_sticker(sample.data, sample.kind, output), output)

    # Тестовые анимации длятся ровно секунду; GIF ограничен 50 fps, остальные форматы хранят все кадры
    expected_fps = min(sample.fps, MAX_GIF_FPS) if output is OutputFormat.GIF else sample.fps
    assert len(frames) == expected_fps
    # WebP хранит длительности кадров в целых миллисекундах
    assert duration_ms == pytest.approx(1000, abs=1)
    first, last = frames[0], frames[-1]
    assert is_transparent(first.getpixel((2, 2)))
    assert is_red(first.getpixel(sample.start))
    assert is_red(last.getpixel(sample.end))
    # Объект уехал: на его прежнем месте прозрачно, «шлейфа» от прошлых кадров нет
    assert is_transparent(last.getpixel(sample.start))


@pytest.mark.parametrize("sample_name", ANIMATED_SAMPLES)
def test_gif_has_no_delays_shorter_than_20ms(request, sample_name):
    sample = request.getfixturevalue(sample_name)
    with Image.open(io.BytesIO(convert_sticker(sample.data, sample.kind, OutputFormat.GIF))) as gif:
        delays = [frame.info["duration"] for frame in ImageSequence.Iterator(gif)]
    # Задержки 10 мс и меньше браузеры растягивают до 100 мс, и анимация начинает тормозить
    assert min(delays) >= 20


@pytest.mark.parametrize(
    ("output", "keeps_semi_transparency"),
    [(OutputFormat.GIF, False), (OutputFormat.APNG, True), (OutputFormat.WEBP, True), (OutputFormat.WEBM, True)],
)
def test_semi_transparency(tgs_sticker, output, keeps_semi_transparency):
    """У сглаженных краёв круга альфа промежуточная; сохранить её может любой формат, кроме GIF."""
    frames, _ = decode(convert_sticker(tgs_sticker.data, StickerKind.ANIMATED, output), output)
    alpha_histogram = frames[0].getchannel("A").histogram()
    assert (sum(alpha_histogram[1:255]) > 0) is keeps_semi_transparency


@pytest.mark.parametrize("output", [OutputFormat.GIF, OutputFormat.APNG])
def test_tgs_edges_are_not_darkened(tgs_sticker, output):
    """rlottie отдаёт premultiplied-альфу: если её не учесть, края круга станут тёмными."""
    frames, _ = decode(convert_sticker(tgs_sticker.data, StickerKind.ANIMATED, output), output)
    visible_colors = [color for _, color in frames[0].getcolors(maxcolors=1 << 16) if color[3] > 0]
    assert visible_colors
    assert all(red > 200 and green < 60 and blue < 60 for red, green, blue, _ in visible_colors)


def test_apng_is_lossless(tgs_sticker):
    frames, _ = decode(convert_sticker(tgs_sticker.data, StickerKind.ANIMATED, OutputFormat.APNG), OutputFormat.APNG)
    with LottieAnimation.from_data(gzip.decompress(tgs_sticker.data).decode()) as animation:
        buffer = animation.lottie_animation_render(frame_num=30)
    rendered = Image.frombuffer("RGBA", (512, 512), buffer, "raw", "BGRa", 0, 1)
    assert frames[30].tobytes() == rendered.tobytes()


@pytest.mark.parametrize("output", [OutputFormat.GIF, OutputFormat.APNG, OutputFormat.WEBP])
def test_animation_loops_forever(tgs_sticker, output):
    with Image.open(io.BytesIO(convert_sticker(tgs_sticker.data, StickerKind.ANIMATED, output))) as image:
        assert image.info["loop"] == 0


def test_webp_background_is_transparent(tgs_sticker):
    """ffmpeg записывает в анимированный WebP белый цвет фона — конвертер должен заменить его прозрачным."""
    webp = convert_sticker(tgs_sticker.data, StickerKind.ANIMATED, OutputFormat.WEBP)
    with Image.open(io.BytesIO(webp)) as image:
        assert image.info["background"] == (0, 0, 0, 0)


def test_video_sticker_to_webm_returns_original(webm_sticker):
    assert convert_sticker(webm_sticker.data, StickerKind.VIDEO, OutputFormat.WEBM) == webm_sticker.data


def test_static_background_does_not_flicker(tgs_over_gradient):
    """Неподвижный градиент под движущимся кругом должен кодироваться одинаково во всех кадрах."""
    frames, _ = decode(convert_sticker(tgs_over_gradient.data, StickerKind.ANIMATED, OutputFormat.GIF), OutputFormat.GIF)
    static_area = (0, 192, 512, 512)  # круг ездит по строкам 68..188
    first = frames[0].crop(static_area).tobytes()
    assert all(frame.crop(static_area).tobytes() == first for frame in frames[1:])


def test_temporary_files_are_removed_after_failure(tgs_sticker, tmp_path, monkeypatch):
    """Даже если кодирование упало посреди перебора кадров, временные файлы должны удалиться."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    # Длительностей меньше, чем кадров: Pillow упадёт на 11-м кадре, когда перебор уже идёт
    monkeypatch.setattr(converter._Frames, "durations_ms", lambda self: [17] * 10)

    with pytest.raises(IndexError):
        convert_sticker(tgs_sticker.data, StickerKind.ANIMATED, OutputFormat.APNG)
    assert not any(tmp_path.iterdir())


def test_vp8_webm_is_converted_with_fallback_decoder(vp8_webm):
    frames, _ = decode(convert_sticker(vp8_webm, StickerKind.VIDEO, OutputFormat.GIF), OutputFormat.GIF)
    assert len(frames) == 10


@pytest.mark.parametrize(("kind", "output"), [(StickerKind.STATIC, OutputFormat.GIF), (StickerKind.ANIMATED, OutputFormat.PNG)])
def test_unsupported_output_raises_value_error(kind, output):
    with pytest.raises(ValueError):
        convert_sticker(b"", kind, output)


@pytest.mark.parametrize("kind", list(StickerKind))
def test_garbage_raises_conversion_error(kind):
    with pytest.raises(ConversionError):
        convert_sticker(b"definitely not a sticker", kind, OUTPUT_FORMATS[kind][0])


@pytest.mark.parametrize("payload", [b"{not json", b'{"not": "lottie"}'])
def test_tgs_with_broken_lottie_raises_conversion_error(payload):
    with pytest.raises(ConversionError):
        convert_sticker(gzip.compress(payload), StickerKind.ANIMATED, OutputFormat.GIF)
