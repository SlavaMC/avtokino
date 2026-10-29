"""Выбор базовых параметров и план обработки каждого файла."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from math import gcd
from pathlib import Path

from .config import DEFAULT_CRF, STANDARD_FPS, Settings
from .naming import is_gopro
from .probe import PhotoInfo, VideoInfo


@dataclass
class Item:
    path: Path
    rel: str
    kind: str                       # video | photo
    info: VideoInfo | PhotoInfo
    when: datetime
    date_source: str


@dataclass
class Base:
    width: int
    height: int
    fps: Fraction
    vcodec: str
    vbitrate: int | None
    crf: int
    source: str                      # откуда взяты параметры

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass
class Segment:
    index: int
    kind: str                        # copy | transcode | photo | title
    duration: float
    item: Item | None = None
    actions: list[str] = field(default_factory=list)
    audio_note: str = ""
    zoom_in: bool = True
    start: float = 0.0               # расчётное начало сегмента в ролике
    caption: bool = False            # на сегмент попадает подпись
    est_bytes: int = 0
    # заполняется при рендере
    video_path: Path | None = None
    audio_path: Path | None = None
    real_duration: float | None = None


def gopro_trim(path: Path, s: Settings) -> float:
    return s.gopro_trim if s.gopro_trim > 0 and is_gopro(path) else 0.0


def fps_str(f: Fraction) -> str:
    return f"{float(f):.3f}".rstrip("0").rstrip(".") if f else "?"


def snap_fps(f: Fraction) -> Fraction:
    if not f:
        return Fraction(30)
    best = min(STANDARD_FPS, key=lambda s: abs(float(s) - float(f)))
    return best if abs(float(best) / float(f) - 1) < 0.01 else f.limit_denominator(1001)


def _even(x: float) -> int:
    return int(round(x / 2)) * 2


def frame_16x9(w: int, h: int) -> tuple[int, int]:
    """Кадр 16:9, в который вписывается видео; вертикальное видео даёт горизонтальный кадр той же высоты по короткой стороне."""
    long_side, short_side = max(w, h), min(w, h)
    if long_side * 9 >= short_side * 16:
        return _even(long_side), _even(long_side * 9 / 16)
    return _even(short_side * 16 / 9), _even(short_side)


def choose_base(videos: list[VideoInfo], s: Settings) -> Base:
    src = max(videos, key=lambda v: (v.width * v.height, v.duration)) if videos else None
    notes = []
    if s.resolution:
        w, h = s.resolution
        notes.append("разрешение из командной строки")
    elif src:
        w, h = frame_16x9(src.width, src.height)
        notes.append(f"разрешение по файлу {src.path.name} ({src.resolution})")
    else:
        w, h = 1920, 1080
        notes.append("видео нет, разрешение по умолчанию")

    if s.fps:
        fps = s.fps
        notes.append("частота из командной строки")
    elif src:
        fps = snap_fps(src.fps)
        notes.append(f"частота по файлу {src.path.name}")
    else:
        fps = Fraction(30)

    if s.vcodec:
        vcodec = s.vcodec
        notes.append("кодек из командной строки")
    elif src and src.vcodec in ("h264", "hevc"):
        vcodec = src.vcodec
        notes.append(f"кодек по файлу {src.path.name}")
    else:
        vcodec = "h264"
        if src:
            notes.append(f"кодек {src.vcodec} не подходит для итогового ролика, взят h264")

    crf = s.crf if s.crf is not None else DEFAULT_CRF[vcodec]
    return Base(w, h, fps, vcodec, s.vbitrate, crf, "; ".join(notes))


def _fps_close(a: Fraction, b: Fraction) -> bool:
    return bool(a) and abs(float(a) / float(b) - 1) < 0.01


def _aspect_note(w: int, h: int, base: Base) -> str | None:
    if w * base.height == h * base.width:
        return None
    g = gcd(w, h)
    ratio = f"{w // g}:{h // g}" if w // g <= 32 else f"{w / h:.2f}:1"
    if w * base.height > h * base.width:
        return f"соотношение {ratio} ≠ 16:9, поля сверху и снизу"
    return f"соотношение {ratio} ≠ 16:9, поля по бокам"


def plan_video(v: VideoInfo, base: Base, s: Settings) -> tuple[str, list[str]]:
    reasons = []
    if v.vcodec != base.vcodec:
        reasons.append(f"кодек: {v.vcodec} → {base.vcodec}")
    if (v.width, v.height) != (base.width, base.height):
        scale = min(base.width / v.width, base.height / v.height)
        reasons.append(f"разрешение: {v.resolution} → вписать в {base.resolution} (масштаб ×{scale:.2f})")
        note = _aspect_note(v.width, v.height, base)
        if note:
            reasons.append(note)
    if v.rotation:
        reasons.append(f"поворот на {v.rotation}°")
    if not _fps_close(v.fps, base.fps):
        reasons.append(f"частота кадров: {fps_str(v.fps)} → {fps_str(base.fps)}")
    if base.vbitrate and v.vbitrate and v.vbitrate > base.vbitrate:
        reasons.append(f"битрейт: {v.vbitrate // 1000} → ≤{base.vbitrate // 1000} кбит/с")
    if v.hdr:
        reasons.append("HDR → SDR (тонмаппинг)")
    if v.pix_fmt != "yuv420p" and not v.hdr:
        reasons.append(f"формат пикселей: {v.pix_fmt} → yuv420p")
    if v.field_order not in ("progressive", "unknown"):
        reasons.append("чересстрочная развёртка → деинтерлейс")
    if v.sar not in ("1:1", "0:1", "N/A"):
        reasons.append(f"неквадратный пиксель (SAR {v.sar})")
    if s.transition != "none":
        reasons.append("переходы включены")
    if s.safe and not reasons:
        reasons.append("режим --safe")
    return ("transcode" if reasons else "copy"), reasons


def audio_note(v: VideoInfo, s: Settings) -> str:
    target = f"PCM {s.arate} Гц {s.achannels} кан. → общая дорожка {s.acodec} {s.abitrate}"
    if not v.has_audio:
        return f"звука нет → тишина; {target}"
    return f"{v.acodec} {v.arate} Гц {v.achannels} кан. → {target}"


def estimate_rate(base: Base, kind: str) -> int:
    if kind == "copy":
        return 0
    px = base.width * base.height / (1920 * 1080)
    rate = 8_000_000 * px * (float(base.fps) / 30) ** 0.5
    if base.vcodec == "hevc":
        rate *= 0.6
    if kind in ("photo", "title"):
        rate *= 0.4
    if base.vbitrate:
        rate = min(rate, base.vbitrate)
    return int(rate)


def photo_durations(items: list[Item], s: Settings) -> list[float]:
    """Серии из более чем N фото подряд получают сокращённую длительность целиком."""
    durs = [0.0] * len(items)
    i = 0
    while i < len(items):
        if items[i].kind != "photo":
            i += 1
            continue
        j = i
        while j < len(items) and items[j].kind == "photo":
            j += 1
        d = s.photo_burst_duration if (j - i) > s.photo_burst_threshold else s.photo_duration
        for k in range(i, j):
            durs[k] = d
        i = j
    return durs


def build_plan(items: list[Item], base: Base, s: Settings) -> list[Segment]:
    segments: list[Segment] = []
    if s.title:
        seg = Segment(0, "title", s.title_duration)
        seg.actions.append(f"титр {base.resolution}")
        seg.audio_note = "тишина"
        segments.append(seg)
    durs = photo_durations(items, s)
    photo_n = 0
    for it, pd in zip(items, durs):
        idx = len(segments)
        if it.kind == "photo":
            p: PhotoInfo = it.info
            seg = Segment(idx, "photo", pd, it, zoom_in=(photo_n % 2 == 0))
            photo_n += 1
            fit = max if s.photo_fit == "cover" else min
            scale = fit(base.width / p.width, base.height / p.height)
            seg.actions.append(f"фото {p.width}x{p.height} → кадр {base.resolution} (масштаб ×{scale:.3f})")
            note = _aspect_note(p.width, p.height, base)
            if note:
                if s.photo_fit == "cover":
                    note = note.split(",")[0] + ", обрезка до 16:9 на весь кадр"
                seg.actions.append(note)
            direction = "приближение" if seg.zoom_in else "отдаление"
            seg.actions.append(f"плавный зум {int(round((s.photo_zoom - 1) * 100))}% ({direction})")
            seg.actions.append(f"кодирование {base.vcodec} {fps_str(base.fps)} к/с")
            seg.audio_note = "тишина"
        else:
            v: VideoInfo = it.info
            kind, reasons = plan_video(v, base, s)
            trim = gopro_trim(it.path, s)
            seg = Segment(idx, kind, v.duration - trim, it)
            if kind == "copy":
                seg.actions.append(f"видео без перекодировки ({v.vcodec} {v.resolution} {fps_str(v.fps)} к/с)")
            else:
                seg.actions.append(f"перекодировка видео в {base.vcodec} {base.resolution} {fps_str(base.fps)} к/с")
                seg.actions.extend(reasons)
            if trim:
                seg.actions.append(f"обрезка конца GoPro: −{trim:g} с ({v.duration:.2f} → {seg.duration:.2f} с)")
            seg.audio_note = audio_note(v, s)
        segments.append(seg)

    start = 0.0
    for seg in segments:
        seg.start = start
        start += seg.duration
        shown = min(seg.start + seg.duration, s.caption_end) - max(seg.start, s.caption_start)
        if s.caption and shown > 0.01:
            seg.caption = True
            seg.actions.append(f"подпись в правом нижнем углу ({shown:.1f} с)")
            if seg.kind == "copy":
                seg.kind = "transcode"
                seg.actions[0] = (f"перекодировка видео в {base.vcodec} {base.resolution} "
                                  f"{fps_str(base.fps)} к/с (из-за подписи)")

    for seg in segments:
        if seg.kind == "copy":
            rate = seg.item.info.vbitrate or estimate_rate(base, "transcode")
        else:
            rate = estimate_rate(base, seg.kind)
        seg.est_bytes = int(rate * seg.duration / 8)
    return segments
