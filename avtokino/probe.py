"""Анализ медиафайлов: ffprobe для видео и звука, Pillow для фото."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageOps

try:  # HEIC/HEIF (iPhone) читаются только с pillow-heif
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover
    HEIF_SUPPORTED = False

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


@dataclass
class VideoInfo:
    path: Path
    duration: float
    width: int            # с учётом поворота (как видит зритель)
    height: int
    rotation: int
    vcodec: str
    profile: str
    pix_fmt: str
    fps: Fraction
    vbitrate: int | None
    sar: str
    field_order: str
    hdr: bool
    has_audio: bool
    acodec: str | None = None
    arate: int | None = None
    achannels: int | None = None
    abitrate: int | None = None
    tags: dict = field(default_factory=dict)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass
class PhotoInfo:
    path: Path
    width: int            # после применения EXIF-ориентации
    height: int
    exif_datetime: str | None = None
    exif_offset: str | None = None
    exif_tag: str | None = None


def _fraction(value: str | None) -> Fraction:
    try:
        f = Fraction(value)
        return f if f > 0 else Fraction(0)
    except (TypeError, ValueError, ZeroDivisionError):
        return Fraction(0)


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            return int(round(float(sd["rotation"]))) % 360
    rot = stream.get("tags", {}).get("rotate")
    return int(rot) % 360 if rot else 0


def ffprobe_json(ffprobe: str, path: Path) -> dict:
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    out = subprocess.run(cmd, capture_output=True, check=True)
    return json.loads(out.stdout.decode("utf-8", errors="replace"))


def probe_duration(ffprobe: str, path: Path) -> float:
    data = ffprobe_json(ffprobe, path)
    return float(data.get("format", {}).get("duration") or 0.0)


def probe_video(ffprobe: str, path: Path) -> VideoInfo | None:
    data = ffprobe_json(ffprobe, path)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    if v is None:
        return None
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})

    rotation = _rotation(v)
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    if rotation in (90, 270):
        w, h = h, w
    fps = _fraction(v.get("avg_frame_rate")) or _fraction(v.get("r_frame_rate"))
    duration = float(v.get("duration") or fmt.get("duration") or 0.0)

    vbitrate = int(v["bit_rate"]) if v.get("bit_rate") else None
    if vbitrate is None and fmt.get("bit_rate"):
        vbitrate = int(fmt["bit_rate"]) - (int(a["bit_rate"]) if a and a.get("bit_rate") else 0)

    tags = {}
    tags.update({k.lower(): val for k, val in (fmt.get("tags") or {}).items()})
    for k, val in (v.get("tags") or {}).items():
        tags.setdefault(k.lower(), val)

    return VideoInfo(
        path=path, duration=duration, width=w, height=h, rotation=rotation,
        vcodec=v.get("codec_name", "?"), profile=v.get("profile", ""),
        pix_fmt=v.get("pix_fmt", ""), fps=fps, vbitrate=vbitrate,
        sar=v.get("sample_aspect_ratio", "1:1") or "1:1",
        field_order=v.get("field_order", "progressive") or "progressive",
        hdr=v.get("color_transfer") in HDR_TRANSFERS,
        has_audio=a is not None,
        acodec=a.get("codec_name") if a else None,
        arate=int(a["sample_rate"]) if a and a.get("sample_rate") else None,
        achannels=a.get("channels") if a else None,
        abitrate=int(a["bit_rate"]) if a and a.get("bit_rate") else None,
        tags=tags,
    )


_EXIF_IFD = 0x8769
_DATE_TAGS = [(36867, 36881, "EXIF DateTimeOriginal"),
              (36868, 36882, "EXIF DateTimeDigitized")]


def probe_photo(path: Path) -> PhotoInfo:
    with Image.open(path) as img:
        exif = img.getexif()
        w, h = img.size
        orientation = exif.get(0x0112, 1)
        if orientation in (5, 6, 7, 8):
            w, h = h, w
        info = PhotoInfo(path=path, width=w, height=h)
        ifd = exif.get_ifd(_EXIF_IFD) if exif else {}
        for tag, off_tag, label in _DATE_TAGS:
            if ifd.get(tag):
                info.exif_datetime = str(ifd[tag]).strip("\x00 ")
                info.exif_offset = str(ifd.get(off_tag) or ifd.get(36880) or "").strip("\x00 ") or None
                info.exif_tag = label
                break
        else:
            if exif.get(306):
                info.exif_datetime = str(exif[306]).strip("\x00 ")
                info.exif_tag = "EXIF DateTime"
        return info


def load_photo(path: Path) -> Image.Image:
    """Открывает фото, применяет EXIF-ориентацию, приводит к RGB."""
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (0, 0, 0))
            bg.paste(img, mask=img.split()[-1])
            return bg
        return img.convert("RGB")
