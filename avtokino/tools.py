"""Служебные команды для отдельных файлов: python -m avtokino.tools <команда> ...

Команды:
  trim   вырезать кусок видео без перекодировки
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .probe import probe_duration


def _num(x: float) -> str:
    """12.0 -> «12», 12.5 -> «12.5»: для имени файла."""
    return f"{x:g}"


def _copy_times(src: Path, dst: Path) -> None:
    """Даты файла как у исходника: изменение и доступ, а в Windows ещё и создание."""
    st = src.stat()
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    if os.name != "nt":
        return
    birth = getattr(st, "st_birthtime_ns", None) or int(getattr(st, "st_ctime", 0) * 1e9)
    if not birth:
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    FILE_WRITE_ATTRIBUTES, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS = 0x100, 3, 0x02000000
    h = k32.CreateFileW(str(dst), FILE_WRITE_ATTRIBUTES, 0, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
    if h in (None, wintypes.HANDLE(-1).value):
        return
    try:
        ft = birth // 100 + 116444736000000000  # FILETIME: сотни нс от 1601 года
        ctime = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
        k32.SetFileTime(h, ctypes.byref(ctime), None, None)
    finally:
        k32.CloseHandle(h)


def trim(args) -> int:
    src = Path(args.file).resolve()
    if not src.is_file():
        print(f"Файл не найден: {src}", file=sys.stderr)
        return 2
    duration = probe_duration(args.ffprobe, src)
    if duration <= 0:
        print(f"Не удалось определить длительность: {src.name}", file=sys.stderr)
        return 2

    if args.start is not None or args.end is not None:
        start = args.start or 0.0
        end = args.end if args.end is not None else duration
        tag = f"{_num(start)}-{_num(end) if args.end is not None else 'end'}"
    else:
        start = args.cut_start or 0.0
        end = duration - (args.cut_end or 0.0)
        parts = ([f"start{_num(args.cut_start)}"] if args.cut_start else []) + \
                ([f"end{_num(args.cut_end)}"] if args.cut_end else [])
        tag = "_".join(parts)
    end = min(end, duration)
    if not tag:
        print("Укажите --start/--end или --cut-start/--cut-end", file=sys.stderr)
        return 2
    if start < 0 or end <= start:
        print(f"Неверный интервал: {start:g}–{end:g} с (длительность файла {duration:.2f} с)", file=sys.stderr)
        return 2

    out = src.with_name(f"{src.stem}_trim_{tag}{src.suffix}")
    if out.exists() and not args.force:
        print(f"Файл уже существует: {out.name} (перезаписать: --force)", file=sys.stderr)
        return 2

    base = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if start > 0:
        base += ["-ss", f"{start:.3f}"]
    base += ["-i", str(src), "-t", f"{end - start:.3f}"]
    tail = ["-c", "copy", "-map_metadata", "0", "-avoid_negative_ts", "make_zero"]
    ext = src.suffix.lower()
    if ext in (".mp4", ".mov", ".m4v"):
        tail += ["-movflags", "use_metadata_tags"]
    # Сначала со всеми потоками (данные GoPro, таймкод); если контейнер их не принимает — только видео и звук.
    # Субтитры PGS в .MTS (наложение даты у Sony) ffmpeg в MPEG-TS записать не умеет, их пропускаем.
    first = ["-map", "0"] + (["-map", "-0:s"] if ext in (".mts", ".m2ts", ".ts") else [])
    attempts = [first, ["-map", "0:v", "-map", "0:a?"]]
    err = ""
    for maps in attempts:
        proc = subprocess.run(base + maps + tail + [str(out)], capture_output=True)
        if proc.returncode == 0:
            break
        err = proc.stderr.decode("utf-8", errors="replace").strip()
    else:
        print(f"Ошибка ffmpeg: {err}", file=sys.stderr)
        return 1

    _copy_times(src, out)
    real = probe_duration(args.ffprobe, out)
    early = real - (end - start)  # без перекодировки кусок начинается с ключевого кадра
    print(f"Готово: {out.name}")
    print(f"  запрошено: {start:.2f}–{end:.2f} с ({end - start:.2f} с)")
    if start > 0 and early > 0.1:
        print(f"  без перекодировки кусок начинается с ближайшего ключевого кадра, "
              f"примерно на {early:.2f} с раньше ({max(0.0, start - early):.2f} с)")
    print(f"  длительность результата: {real:.2f} с")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="avtokino.tools", description="Служебные команды для файлов.")
    p.add_argument("--ffmpeg", default="ffmpeg", help="путь к ffmpeg")
    p.add_argument("--ffprobe", default="ffprobe", help="путь к ffprobe")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("trim", help="вырезать кусок видео без перекодировки",
                       description="Вырезает кусок видео без перекодировки, сохраняя метаданные и дату файла. "
                                   "Результат: <имя>_trim_<параметры>.<расширение> рядом с исходником.")
    t.add_argument("file", help="видеофайл")
    t.add_argument("--start", type=float, metavar="СЕК", help="начало куска, с")
    t.add_argument("--end", type=float, metavar="СЕК", help="конец куска, с")
    t.add_argument("--cut-start", type=float, metavar="СЕК", help="сколько секунд отрезать от начала")
    t.add_argument("--cut-end", type=float, metavar="СЕК", help="сколько секунд отрезать от конца")
    t.add_argument("--force", action="store_true", help="перезаписать существующий результат")
    t.set_defaults(func=trim)

    a = p.parse_args(argv)
    if a.command == "trim" and (a.start is not None or a.end is not None) and (a.cut_start or a.cut_end):
        p.error("trim: используйте либо --start/--end, либо --cut-start/--cut-end")
    for tool in (a.ffmpeg, a.ffprobe):
        if shutil.which(tool) is None and not Path(tool).is_file():
            print(f"Не найден {tool}", file=sys.stderr)
            return 2
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
