"""Тестовые стикеры всех трёх типов, которые генерируются на лету."""

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
    """Анимированный стикер: круг диаметром 120 едет из x=100 в x=412 за 1 секунду, 60 fps."""
    circle = {
        "ty": 4, "ind": 1, "ip": 0, "op": 60, "st": 0, "sr": 1, "ao": 0, "ddd": 0, "bm": 0,
        "ks": {
            "o": {"a": 0, "k": 100},
            "r": {"a": 0, "k": 0},
            "a": {"a": 0, "k": [0, 0, 0]},
            "s": {"a": 0, "k": [100, 100, 100]},
            "p": {"a": 1, "k": [
                {"t": 0, "s": [100, 256, 0], "o": {"x": [0], "y": [0]}, "i": {"x": [1], "y": [1]}},
                {"t": 60, "s": [412, 256, 0]},
            ]},
        },
        "shapes": [{"ty": "gr", "it": [
            {"ty": "el", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [120, 120]}},
            {"ty": "fl", "c": {"a": 0, "k": [1, 0, 0, 1]}, "o": {"a": 0, "k": 100}},
            {"ty": "tr", "p": {"a": 0, "k": [0, 0]}, "a": {"a": 0, "k": [0, 0]},
             "s": {"a": 0, "k": [100, 100]}, "r": {"a": 0, "k": 0}, "o": {"a": 0, "k": 100}},
        ]}],
    }
    lottie = {"tgs": 1, "v": "5.5.2", "fr": 60, "ip": 0, "op": 60, "w": 512, "h": 512,
              "ddd": 0, "assets": [], "layers": [circle]}
    return AnimatedSample(
        data=gzip.compress(json.dumps(lottie).encode()),
        frame_count=30,  # 60 fps прореживаются до 30
        duration_ms=1000,
        start=(100, 256),
        end=(400, 256),  # последний кадр GIF — 58-й кадр анимации, центр круга на x≈402
    )


@pytest.fixture(scope="session")
def webm_sticker(tmp_path_factory: pytest.TempPathFactory) -> AnimatedSample:
    """Видеостикер: квадрат 64x64 едет по прозрачному фону 256x256, VP9 с альфой, 30 fps, 1 с."""
    path = tmp_path_factory.mktemp("stickers") / "sticker.webm"
    _ffmpeg(
        "-filter_complex",
        "color=c=black@0:s=256x256:r=30:d=1,format=rgba[bg];"
        "color=c=red:s=64x64:r=30:d=1,format=rgba[fg];"
        "[bg][fg]overlay=x='10+t*150':y=96:shortest=1,format=yuva420p[out]",
        "-map", "[out]", "-c:v", "libvpx-vp9", "-auto-alt-ref", "0", "-b:v", "0", "-crf", "30",
        str(path),
    )
    return AnimatedSample(
        data=path.read_bytes(),
        frame_count=30,
        duration_ms=1000,
        start=(42, 128),  # на первом кадре квадрат занимает x 10..74
        end=(187, 128),  # на последнем (t = 29/30 с) — x 155..219
    )


@pytest.fixture(scope="session")
def vp8_webm(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """WEBM в кодеке VP8 — Telegram такие не выпускает, но конвертер должен справиться."""
    path = tmp_path_factory.mktemp("stickers") / "vp8.webm"
    _ffmpeg("-f", "lavfi", "-i", "testsrc=s=64x64:r=10:d=1", "-c:v", "libvpx", str(path))
    return path.read_bytes()


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", *args],
        check=True,
        capture_output=True,
    )
