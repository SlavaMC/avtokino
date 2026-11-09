"""Разбор длительностей в параметрах: секунды (90, 2.5) или ЧЧ:ММ:СС / ММ:СС (1:30, 01:02:03.5)."""
from __future__ import annotations

import argparse
import re

_RE = re.compile(r"\s*([+\-−]?)\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:[.,]\d*)?|[.,]\d+)\s*")
FORMAT = "секунды или ЧЧ:ММ:СС"


def parse_duration(value: str, signed: bool = False) -> float:
    """«90» → 90, «1:30» → 90, «01:02:03.5» → 3723.5, «-0:30» → −30 (если signed)."""
    m = _RE.fullmatch(str(value))
    if not m:
        raise ValueError(f"ожидаются {FORMAT} (например 90, 1:30 или 01:02:03), получено: {value}")
    sign = -1.0 if m[1] in ("-", "−") else 1.0
    if sign < 0 and not signed:
        raise ValueError(f"длительность не может быть отрицательной: {value}")
    sec = float(m[4].replace(",", "."))
    hh, mm = (m[2], m[3]) if m[3] is not None else (None, m[2])
    if mm is not None:
        if sec >= 60:
            raise ValueError(f"секунд должно быть меньше 60: {value}")
        sec += int(mm) * 60
        if hh is not None:
            if int(mm) >= 60:
                raise ValueError(f"минут должно быть меньше 60: {value}")
            sec += int(hh) * 3600
    return sign * sec


def seconds(value: str) -> float:
    """Тип для argparse: неотрицательная длительность в секундах."""
    try:
        return parse_duration(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def signed_seconds(value: str) -> float:
    try:
        return parse_duration(value, signed=True)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def hours(value: str) -> float:
    """Тип для argparse: часы числом (6, 1.5) или ЧЧ:ММ[:СС] (1:30 = полтора часа)."""
    v = str(value).strip()
    try:
        if ":" not in v:
            return parse_duration(v)
        parts = v.split(":")
        if len(parts) == 2:          # ЧЧ:ММ
            v += ":00"
        return parse_duration(v) / 3600
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e).replace("ЧЧ:ММ:СС", "ЧЧ:ММ")) from None
