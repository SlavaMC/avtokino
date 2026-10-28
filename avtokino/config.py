"""Параметры запуска и значения по умолчанию."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".3gp",
              ".mpg", ".mpeg", ".wmv", ".webm"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"}
MUSIC_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
IGNORED_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}

# Стандартные частоты кадров, к которым «прилипает» частота базового файла.
STANDARD_FPS = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
                Fraction(30), Fraction(50), Fraction(60000, 1001), Fraction(60)]

ENCODERS = {"h264": "libx264", "hevc": "libx265"}
DEFAULT_CRF = {"h264": 20, "hevc": 22}


@dataclass
class Settings:
    workdir: Path
    resolution: tuple[int, int] | None = None
    fps: Fraction | None = None
    vcodec: str | None = None
    vbitrate: int | None = None          # бит/с, потолок; None = только CRF
    crf: int | None = None
    preset: str = "medium"
    acodec: str = "aac"
    abitrate: str = "192k"
    arate: int = 48000
    achannels: int = 2
    volume: float = 1.0
    music_dir: Path | None = None
    music_volume: float = 0.3
    music_crossfade: float = 3.0
    music_fadeout: float = 5.0
    ducking: bool = True
    photo_duration: float = 5.0
    photo_burst_duration: float = 3.0
    photo_burst_threshold: int = 5
    photo_zoom: float = 1.05
    min_clip: float = 5.0
    tz: str | None = None
    transition: str = "none"
    transition_duration: float = 0.5
    title: bool = False
    title_duration: float = 4.0
    jobs: int = 2
    temp_dir: Path | None = None
    keep_temp: bool = False
    dry_run: bool = False
    safe: bool = False
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    verbose: bool = False
