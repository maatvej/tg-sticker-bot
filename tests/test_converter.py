import gzip
import io

import pytest
from PIL import Image, ImageSequence

from sticker_bot.converter import ConversionError, tgs_to_gif, webm_to_gif, webp_to_png


def gif_frames(data: bytes) -> tuple[list[Image.Image], list[int]]:
    """Кадры GIF в том виде, в каком их покажет просмотрщик, и их длительности в мс."""
    with Image.open(io.BytesIO(data)) as gif:
        assert gif.format == "GIF"
        frames, durations = [], []
        for frame in ImageSequence.Iterator(gif):
            frames.append(frame.convert("RGBA"))
            durations.append(frame.info["duration"])
    return frames, durations


def is_red(pixel: tuple[int, int, int, int]) -> bool:
    red, green, blue, alpha = pixel
    return alpha == 255 and red > 200 and green < 60 and blue < 60


def test_static_sticker_becomes_png_with_transparency(webp_sticker):
    with Image.open(io.BytesIO(webp_to_png(webp_sticker.data))) as image:
        assert image.format == "PNG"
        assert image.size == (512, 512)
        image = image.convert("RGBA")
        assert image.getpixel((5, 5))[3] == 0
        assert image.getpixel((256, 256)) == (255, 0, 0, 255)


@pytest.mark.parametrize(
    ("convert", "sample_name"),
    [
        (tgs_to_gif, "tgs_sticker"),
        (tgs_to_gif, "tgs_sticker_30fps"),
        (webm_to_gif, "webm_sticker"),
        (webm_to_gif, "webm_sticker_60fps"),
    ],
)
def test_animated_sticker_becomes_transparent_gif(request, convert, sample_name):
    sample = request.getfixturevalue(sample_name)
    frames, durations = gif_frames(convert(sample.data))

    assert len(frames) == sample.frame_count
    assert sum(durations) == sample.duration_ms
    # Задержки 10 мс и меньше браузеры растягивают до 100 мс, и анимация начинает тормозить
    assert min(durations) >= 20
    first, last = frames[0], frames[-1]
    assert first.getpixel((2, 2))[3] == 0
    assert is_red(first.getpixel(sample.start))
    assert is_red(last.getpixel(sample.end))
    # Объект уехал: на его прежнем месте прозрачно, «шлейфа» от прошлых кадров нет
    assert last.getpixel(sample.start)[3] == 0


def test_tgs_edges_are_not_darkened(tgs_sticker):
    """rlottie отдаёт premultiplied-альфу: если её не учесть, края круга станут тёмными."""
    frames, _ = gif_frames(tgs_to_gif(tgs_sticker.data))
    opaque_colors = [color for _, color in frames[0].getcolors(maxcolors=256) if color[3] == 255]
    assert opaque_colors
    assert all(is_red(color) for color in opaque_colors)


def test_static_background_does_not_flicker(tgs_over_gradient):
    """Неподвижный градиент под движущимся кругом должен кодироваться одинаково во всех кадрах."""
    frames, _ = gif_frames(tgs_to_gif(tgs_over_gradient.data))
    static_area = (0, 192, 512, 512)  # круг ездит по строкам 68..188
    first = frames[0].crop(static_area).tobytes()
    assert all(frame.crop(static_area).tobytes() == first for frame in frames[1:])


def test_vp8_webm_is_converted_with_fallback_decoder(vp8_webm):
    frames, _ = gif_frames(webm_to_gif(vp8_webm))
    assert len(frames) == 10


@pytest.mark.parametrize("convert", [webp_to_png, tgs_to_gif, webm_to_gif])
def test_garbage_raises_conversion_error(convert):
    with pytest.raises(ConversionError):
        convert(b"definitely not a sticker")


@pytest.mark.parametrize("payload", [b"{not json", b'{"not": "lottie"}'])
def test_tgs_with_broken_lottie_raises_conversion_error(payload):
    with pytest.raises(ConversionError):
        tgs_to_gif(gzip.compress(payload))
