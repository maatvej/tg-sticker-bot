"""Тестовые стикеры, которые генерируются на лету."""

import gzip
import io
import json
import subprocess
from dataclasses import dataclass

import imageio_ffmpeg
import pytest
from PIL import Image


@dataclass(frozen=True)
class StickerSample:
    data: bytes


@dataclass(frozen=True)
class AnimatedSample(StickerSample):
    """Анимация, в которой красный объект едет слева направо по прозрачному фону."""

    frame_count: int  # сколько кадров должно получиться в GIF
    duration_ms: int  # какой должна получиться длительность GIF
    start: tuple[int, int]  # точка внутри объекта на первом кадре
    end: tuple[int, int]  # точка внутри объекта на последнем кадре


@pytest.fixture(scope="session")
def webp_sticker() -> StickerSample:
    """Статичный стикер: красный квадрат в центре прозрачного холста 512x512."""
    image = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (156, 156, 356, 356))
    output = io.BytesIO()
    image.save(output, format="WEBP", lossless=True)
    return StickerSample(output.getvalue())


@pytest.fixture(scope="session")
def tgs_sticker() -> AnimatedSample:
    """Анимированный стикер 60 fps, как большинство TGS: в GIF он пересэмплируется в 50 fps."""
    return AnimatedSample(
        data=_make_tgs(fps=60, layers=[_moving_circle(frames=60)]),
        frame_count=50,
        duration_ms=1000,
        start=(100, 256),
        end=(400, 256),  # к концу анимации центр круга доезжает до x≈405
    )


@pytest.fixture(scope="session")
def tgs_sticker_30fps() -> AnimatedSample:
    """Анимированный стикер 30 fps: частота в GIF сохраняется."""
    return AnimatedSample(
        data=_make_tgs(fps=30, layers=[_moving_circle(frames=30)]),
        frame_count=30,
        duration_ms=1000,
        start=(100, 256),
        end=(400, 256),
    )


@pytest.fixture(scope="session")
def tgs_over_gradient() -> StickerSample:
    """Круг едет по строкам 68..188 поверх неподвижного фона из двух перпендикулярных градиентов.

    Цветов в таком фоне больше, чем вмещает палитра GIF, поэтому без дизеринга не обойтись.
    """
    return StickerSample(_make_tgs(fps=60, layers=[
        _moving_circle(frames=60, y=128),
        _gradient_layer(index=2, frames=60, start=[-256, 0], end=[256, 0], colors=[0, 1, 0, 0, 1, 0, 1, 0], opacity=50),
        _gradient_layer(index=3, frames=60, start=[0, -256], end=[0, 256], colors=[0, 0, 0, 1, 1, 1, 1, 0]),
    ]))


@pytest.fixture(scope="session")
def webm_sticker(tmp_path_factory: pytest.TempPathFactory) -> AnimatedSample:
    """Видеостикер 30 fps — максимум, который допускает Telegram: частота в GIF сохраняется."""
    return AnimatedSample(
        data=_make_webm(tmp_path_factory, fps=30),
        frame_count=30,
        duration_ms=1000,
        start=(42, 128),  # на первом кадре квадрат занимает x 10..74
        end=(187, 128),  # на последнем — примерно x 155..219
    )


@pytest.fixture(scope="session")
def webm_sticker_60fps(tmp_path_factory: pytest.TempPathFactory) -> AnimatedSample:
    """Видеостикер 60 fps: Telegram такие не выпускает, но в GIF он должен стать 50 fps."""
    return AnimatedSample(
        data=_make_webm(tmp_path_factory, fps=60),
        frame_count=50,
        duration_ms=1000,
        start=(42, 128),
        end=(187, 128),
    )


@pytest.fixture(scope="session")
def vp8_webm(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """WEBM в кодеке VP8 — Telegram такие не выпускает, но конвертер должен справиться."""
    path = tmp_path_factory.mktemp("stickers") / "vp8.webm"
    _ffmpeg("-f", "lavfi", "-i", "testsrc=s=64x64:r=10:d=1", "-c:v", "libvpx", str(path))
    return path.read_bytes()


def _make_tgs(fps: int, layers: list[dict]) -> bytes:
    """TGS длиной 1 секунду: Lottie JSON, сжатый gzip."""
    lottie = {"tgs": 1, "v": "5.5.2", "fr": fps, "ip": 0, "op": fps, "w": 512, "h": 512,
              "ddd": 0, "assets": [], "layers": layers}
    return gzip.compress(json.dumps(lottie).encode())


def _moving_circle(frames: int, y: int = 256) -> dict:
    """Красный круг диаметром 120 равномерно едет из x=100 в x=412 за всю анимацию."""
    position = {"a": 1, "k": [
        {"t": 0, "s": [100, y, 0], "o": {"x": [0], "y": [0]}, "i": {"x": [1], "y": [1]}},
        {"t": frames, "s": [412, y, 0]},
    ]}
    return _layer(index=1, frames=frames, position=position, shapes=[
        {"ty": "el", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [120, 120]}},
        {"ty": "fl", "c": {"a": 0, "k": [1, 0, 0, 1]}, "o": {"a": 0, "k": 100}},
    ])


def _gradient_layer(
    index: int, frames: int, start: list[int], end: list[int], colors: list[float], opacity: int = 100
) -> dict:
    """Неподвижный прямоугольник во весь холст с линейным градиентом из start в end.

    colors — две точки градиента в формате Lottie: [позиция, r, g, b, позиция, r, g, b].
    """
    position = {"a": 0, "k": [256, 256, 0]}
    return _layer(index=index, frames=frames, position=position, opacity=opacity, shapes=[
        {"ty": "rc", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [512, 512]}, "r": {"a": 0, "k": 0}},
        {"ty": "gf", "t": 1, "r": 1, "o": {"a": 0, "k": 100},
         "s": {"a": 0, "k": start}, "e": {"a": 0, "k": end},
         "g": {"p": 2, "k": {"a": 0, "k": colors}}},
    ])


def _layer(index: int, frames: int, position: dict, shapes: list[dict], opacity: int = 100) -> dict:
    group_transform = {"ty": "tr", "p": {"a": 0, "k": [0, 0]}, "a": {"a": 0, "k": [0, 0]},
                       "s": {"a": 0, "k": [100, 100]}, "r": {"a": 0, "k": 0}, "o": {"a": 0, "k": 100}}
    return {
        "ty": 4, "ind": index, "ip": 0, "op": frames, "st": 0, "sr": 1, "ao": 0, "ddd": 0, "bm": 0,
        "ks": {"o": {"a": 0, "k": opacity}, "r": {"a": 0, "k": 0}, "a": {"a": 0, "k": [0, 0, 0]},
               "s": {"a": 0, "k": [100, 100, 100]}, "p": position},
        "shapes": [{"ty": "gr", "it": [*shapes, group_transform]}],
    }


def _make_webm(tmp_path_factory: pytest.TempPathFactory, fps: int) -> bytes:
    """WEBM (VP9 с альфой) длиной 1 секунду: квадрат 64x64 едет вправо по прозрачному фону 256x256."""
    path = tmp_path_factory.mktemp("stickers") / f"sticker_{fps}fps.webm"
    _ffmpeg(
        "-filter_complex",
        f"color=c=black@0:s=256x256:r={fps}:d=1,format=rgba[bg];"
        f"color=c=red:s=64x64:r={fps}:d=1,format=rgba[fg];"
        "[bg][fg]overlay=x='10+t*150':y=96:shortest=1,format=yuva420p[out]",
        "-map", "[out]", "-c:v", "libvpx-vp9", "-auto-alt-ref", "0", "-b:v", "0", "-crf", "30",
        str(path),
    )
    return path.read_bytes()


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", *args],
        check=True,
        capture_output=True,
    )
