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
    aspect: tuple[int, int] | None = None     # соотношение сторон кадра, например (4, 3)
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
    music_shuffle: bool = False          # брать треки в случайном порядке
    ducking: bool = True
    photo_duration: float = 5.0
    photo_burst_duration: float = 3.0
    photo_burst_threshold: int = 5
    photo_zoom: float = 1.05
    min_clip: float = 5.0
    long_video: float = 60.0             # видео длиннее этого перечисляются в логе отдельно
    exclude: list[str] | None = None     # каталоги, которые не обрабатываются (имя, путь или маска)
    gopro_trim: float = 0.0              # сколько секунд отрезать в конце роликов GoPro
    photo_fit: str = "contain"           # contain = целиком с полями, cover = на весь кадр с обрезкой
    caption: bool = False
    caption_name: str | None = None
    caption_date: str | None = None
    caption_duration: float = 10.0
    caption_font: str | None = None
    tz: str | None = None
    date_tolerance: float = 6.0          # часов запаса вокруг периода события
    time_fixes: list | None = None       # поправки сбитых часов (timedelta), пробуются по порядку
    shifts: list | None = None           # сдвиги даты по маске файла: [(маска, секунды)]
    transition: str = "none"
    transition_duration: float = 0.5
    title: bool = False
    title_duration: float = 4.0
    title_text: list[str] | None = None  # свои строки титра вместо названия и даты из каталога
    jobs: int = 2
    temp_dir: Path | None = None
    keep_temp: bool = False
    dry_run: bool = False
    safe: bool = False
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    verbose: bool = False

    @property
    def caption_start(self) -> float:
        """Подпись появляется после титра, если он есть."""
        return self.title_duration if self.title else 0.0

    @property
    def caption_end(self) -> float:
        return self.caption_start + self.caption_duration
