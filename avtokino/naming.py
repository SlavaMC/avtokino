"""Разбор имени рабочего каталога и выбор имени итогового ролика."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

# YYYYMM, YYYYMMDD, YYYYMMXX, YYYYMMDD-DD, YYYYMMDD-MMDD, затем _Название
_DIR_RE = re.compile(
    r"^(?P<y>\d{4})(?P<m>\d{2})(?:(?P<d>\d{2}|XX)(?:-(?P<end>\d{4}|\d{2}))?)?(?:_(?P<name>.*))?$",
    re.IGNORECASE,
)


@dataclass
class EventInfo:
    raw: str
    name: str
    start: date | None = None
    end: date | None = None
    month_only: bool = False

    def describe(self) -> str:
        if self.start is None:
            return "дата в названии не распознана"
        if self.month_only:
            return self.start.strftime("%m.%Y") + " (день неизвестен)"
        if self.end and self.end != self.start:
            return f"{self.start:%d.%m.%Y} – {self.end:%d.%m.%Y}"
        return f"{self.start:%d.%m.%Y}"

    def title_date(self) -> str:
        if self.start is None:
            return ""
        if self.month_only:
            return f"{self.start:%m.%Y}"
        if self.end and self.end != self.start:
            if (self.end.year, self.end.month) == (self.start.year, self.start.month):
                return f"{self.start:%d}–{self.end:%d.%m.%Y}"
            return f"{self.start:%d.%m.%Y} – {self.end:%d.%m.%Y}"
        return f"{self.start:%d.%m.%Y}"

    def period(self, tolerance: timedelta = timedelta(0)) -> tuple[datetime, datetime] | None:
        """Границы события [начало, конец) с запасом; для «только месяц» — весь месяц."""
        if self.start is None:
            return None
        begin = datetime.combine(self.start, datetime.min.time())
        if self.month_only:
            nxt = date(self.start.year + self.start.month // 12, self.start.month % 12 + 1, 1)
        else:
            nxt = (self.end or self.start) + timedelta(days=1)
        return begin - tolerance, datetime.combine(nxt, datetime.min.time()) + tolerance

    def caption_date(self) -> str:
        """Дата для подписи: «Август 2014», «15 июня 2024», «31 июля – 3 августа 2014»."""
        s, e = self.start, self.end
        if s is None:
            return ""
        if self.month_only:
            return f"{MONTHS_NOM[s.month - 1]} {s.year}"
        if not e or e == s:
            return f"{s.day} {MONTHS_GEN[s.month - 1]} {s.year}"
        if (s.year, s.month) == (e.year, e.month):
            return f"{s.day}–{e.day} {MONTHS_GEN[s.month - 1]} {s.year}"
        if s.year == e.year:
            return f"{s.day} {MONTHS_GEN[s.month - 1]} – {e.day} {MONTHS_GEN[e.month - 1]} {s.year}"
        return f"{s.day} {MONTHS_GEN[s.month - 1]} {s.year} – {e.day} {MONTHS_GEN[e.month - 1]} {e.year}"


MONTHS_NOM = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
              "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]

# GoPro: GOPR0123 (HERO3-4), GP010123 (продолжения), GH010123 / GX010123 (HERO5 и новее)
_GOPRO_RE = re.compile(r"^(GOPR\d{4}|GP\d{6}|G[HX]\d{6})$", re.IGNORECASE)


def is_gopro(path: Path) -> bool:
    return bool(_GOPRO_RE.match(path.stem))


def parse_dir_name(raw: str) -> EventInfo:
    m = _DIR_RE.match(raw)
    if not m:
        return EventInfo(raw=raw, name=raw.replace("_", " "))
    name = (m["name"] or "").replace("_", " ").strip()
    y, mo = int(m["y"]), int(m["m"])
    try:
        if m["d"] is None or m["d"].upper() == "XX":
            return EventInfo(raw, name, date(y, mo, 1), None, month_only=True)
        start = date(y, mo, int(m["d"]))
        end = start
        if m["end"]:
            e = m["end"]
            if len(e) == 2:
                end = date(y, mo, int(e))
            else:
                end = date(y, int(e[:2]), int(e[2:]))
            if end < start:  # период через Новый год: 20241230-0102
                end = end.replace(year=y + 1)
        return EventInfo(raw, name, start, end)
    except ValueError:
        return EventInfo(raw=raw, name=raw.replace("_", " "))


def output_pattern(base: str) -> re.Pattern:
    """Имена ранее собранных роликов: <каталог> или <каталог>_vN (любое расширение)."""
    return re.compile(rf"^{re.escape(base)}(?:_v\d+)?$", re.IGNORECASE)


def choose_output(workdir: Path, ext: str = ".mp4") -> Path:
    base = workdir.name
    candidate = workdir / f"{base}{ext}"
    n = 2
    while candidate.exists():
        candidate = workdir / f"{base}_v{n}{ext}"
        n += 1
    return candidate
