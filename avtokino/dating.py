"""Определение даты съёмки и часового пояса события.

Приоритет источников: метаданные файла -> дата в имени файла -> время изменения файла.
Даты бывают трёх видов:
  * local - местное время без пояса (EXIF без смещения, имя файла);
  * aware - время со смещением (EXIF OffsetTime, QuickTime creationdate);
  * utc   - абсолютное время (creation_time в видео, mtime, unix-метка в имени).
Чтобы всё выстроить на одной шкале, определяется смещение события: по файлам,
где оно известно явно, и по видео, у которых есть и UTC-метка, и местное время в имени.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .probe import PhotoInfo, VideoInfo

MIN_YEAR, MAX_YEAR = 1990, 2100


@dataclass
class Evidence:
    value: datetime
    kind: str            # local | aware | utc
    source: str
    has_time: bool = True


@dataclass
class DateResult:
    when: datetime       # местное время события, без tzinfo
    source: str


def _valid(dt: datetime) -> bool:
    return MIN_YEAR <= dt.year <= MAX_YEAR


# --- метаданные -------------------------------------------------------------

def _parse_exif(value: str, offset: str | None) -> datetime | None:
    try:
        dt = datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    if offset:
        m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", offset.strip())
        if m:
            sign = 1 if m[1] == "+" else -1
            dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=int(m[2]), minutes=int(m[3]))))
    return dt


def _parse_iso(value: str) -> datetime | None:
    v = value.strip().replace("Z", "+00:00")
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v)  # +0300 -> +03:00
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt


def photo_meta(info: PhotoInfo) -> Evidence | None:
    if not info.exif_datetime:
        return None
    dt = _parse_exif(info.exif_datetime, info.exif_offset)
    if dt is None or not _valid(dt):
        return None
    if dt.tzinfo:
        return Evidence(dt, "aware", f"{info.exif_tag} ({info.exif_offset})")
    return Evidence(dt, "local", info.exif_tag or "EXIF")


def video_meta(info: VideoInfo) -> Evidence | None:
    apple = info.tags.get("com.apple.quicktime.creationdate")
    if apple:
        dt = _parse_iso(apple)
        if dt and _valid(dt):
            return Evidence(dt, "aware" if dt.tzinfo else "local", "QuickTime creationdate")
    ct = info.tags.get("creation_time")
    if ct:
        dt = _parse_iso(ct)
        if dt and _valid(dt):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return Evidence(dt, "utc", "метаданные creation_time (UTC)")
    return None


# --- имя файла --------------------------------------------------------------

_NAME_DT = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[ _T.-]{0,3}(\d{2})[-_.:h]?(\d{2})[-_.:m]?(\d{2})(?!\d)")
_NAME_D = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")
_NAME_UNIX_MS = re.compile(r"(?<!\d)(1[3-9]\d{11})(?!\d)")


def name_date(path: Path) -> Evidence | None:
    stem = path.stem
    m = _NAME_DT.search(stem)
    if m:
        try:
            dt = datetime(*(int(x) for x in m.groups()))
            if _valid(dt):
                return Evidence(dt, "local", "имя файла")
        except ValueError:
            pass
    m = _NAME_D.search(stem)
    if m:
        try:
            dt = datetime(*(int(x) for x in m.groups()), 12, 0, 0)
            if _valid(dt):
                return Evidence(dt, "local", "имя файла (только дата)", has_time=False)
        except ValueError:
            pass
    m = _NAME_UNIX_MS.search(stem)
    if m:
        dt = datetime.fromtimestamp(int(m[1]) / 1000, tz=timezone.utc)
        if _valid(dt):
            return Evidence(dt, "utc", "имя файла (unix-время)")
    return None


def fs_date(path: Path) -> Evidence:
    st = path.stat()
    # Время создания есть только в Windows; на Linux берём время изменения,
    # оно обычно сохраняется при копировании.
    ts = getattr(st, "st_birthtime", None)
    label = "дата создания файла"
    if ts is None or ts > st.st_mtime:
        ts, label = st.st_mtime, "дата изменения файла"
    return Evidence(datetime.fromtimestamp(ts, tz=timezone.utc), "utc", label)


# --- часовой пояс события ---------------------------------------------------

def _round_offset(delta: timedelta) -> timedelta | None:
    minutes = delta.total_seconds() / 60
    rounded = round(minutes / 15) * 15
    if abs(minutes - rounded) > 2 or abs(rounded) > 14 * 60:
        return None
    return timedelta(minutes=rounded)


def estimate_offset(items: list[tuple[Evidence | None, Evidence | None]]) -> tuple[timedelta, int] | None:
    """items: пары (метаданные, имя файла). Возвращает (смещение, число голосов)."""
    votes: Counter[timedelta] = Counter()
    for meta, name in items:
        if meta and meta.kind == "aware":
            votes[meta.value.utcoffset()] += 1
        elif meta and meta.kind == "utc" and name and name.kind == "local" and name.has_time:
            off = _round_offset(name.value - meta.value.replace(tzinfo=None))
            if off is not None:
                votes[off] += 1
    if not votes:
        return None
    off, n = votes.most_common(1)[0]
    return off, n


def to_local(ev: Evidence, offset: timedelta) -> datetime:
    if ev.kind == "local":
        return ev.value
    return (ev.value.astimezone(timezone.utc) + offset).replace(tzinfo=None)


def resolve(meta: Evidence | None, name: Evidence | None, fs: Evidence, offset: timedelta) -> DateResult:
    if meta:
        return DateResult(to_local(meta, offset), meta.source)
    if name:
        local = to_local(name, offset)
        if not name.has_time:
            fs_local = to_local(fs, offset)
            if fs_local.date() == local.date():
                return DateResult(fs_local, f"{name.source} + время из файловой системы")
        return DateResult(local, name.source)
    return DateResult(to_local(fs, offset), fs.source)
