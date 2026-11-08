"""Служебные команды для отдельных файлов: python -m avtokino.tools <команда> ...

Команды:
  trim        вырезать кусок видео без перекодировки
  timelapse   собрать ролик из последовательности фото, одно фото = один кадр
  rotate180   перевернуть видео на 180° без перекодировки
  sync        рассинхронизация двух роликов с разных камер по звуку
  multicam    общий ролик из двух камер с переключением каждые N секунд
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
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


# --- timelapse ----------------------------------------------------------------

def _natural_key(p: Path):
    """IMG_9.jpg раньше IMG_10.jpg."""
    import re
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", p.name)]


def _photo_time_utc(path: Path):
    """Время съёмки фото в UTC (EXIF со смещением, иначе EXIF как местное время этого компьютера)."""
    from datetime import timezone
    from .dating import photo_meta
    from .probe import probe_photo
    try:
        ev = photo_meta(probe_photo(path))
    except Exception:  # noqa: BLE001
        ev = None
    if ev is None:
        return None
    dt = ev.value if ev.value.tzinfo else ev.value.astimezone()  # без пояса: пояс этого компьютера
    return dt.astimezone(timezone.utc)


def _parse_resolution(value: str) -> tuple[int, int]:
    import re
    m = re.fullmatch(r"\s*(\d+)\s*[xх×:]\s*(\d+)\s*", value, re.IGNORECASE)
    if not m:
        raise argparse.ArgumentTypeError(f"ожидается ШИРИНАxВЫСОТА, например 1920x1080: {value}")
    return int(m[1]), int(m[2])


def timelapse(args) -> int:
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta

    from PIL import Image

    from .config import PHOTO_EXTS
    from .naming import choose_output
    from .probe import load_photo

    folder = Path(args.dir).resolve()
    if not folder.is_dir():
        print(f"Каталог не найден: {folder}", file=sys.stderr)
        return 2
    photos = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_EXTS),
                    key=_natural_key)
    if not photos:
        print(f"В каталоге нет фотографий: {folder}", file=sys.stderr)
        return 2

    first = load_photo(photos[0])
    w, h = args.resolution or first.size
    w, h = w // 2 * 2, h // 2 * 2  # для yuv420p нужны чётные размеры
    out = choose_output(folder, ".mp4")
    n = len(photos)
    print(f"Фото: {n}, первое {photos[0].name}, последнее {photos[-1].name}")
    print(f"Ролик: {out.name}, {w}x{h}, {args.fps:g} кадр/с, {n / args.fps:.1f} с, до {args.max_bitrate:g} Мбит/с")
    if w * h > 4096 * 2304:
        print("  внимание: кадр больше 4K, не все плееры такое воспроизведут; уменьшить: --resolution 3840x2160")

    def frame(path: Path) -> bytes:
        try:
            img = load_photo(path)
        except Exception as e:  # noqa: BLE001
            print(f"  пропуск {path.name}: {e}; вместо него повторён предыдущий кадр", file=sys.stderr)
            return b""
        if img.size != (w, h):
            # Вписываем целиком, лишнее место — чёрные поля.
            k = min(w / img.width, h / img.height)
            size = (max(1, round(img.width * k)), max(1, round(img.height * k)))
            img = img.resize(size, Image.LANCZOS)
            if size != (w, h):
                canvas = Image.new("RGB", (w, h))
                canvas.paste(img, ((w - size[0]) // 2, (h - size[1]) // 2))
                img = canvas
        return img.tobytes()

    rate = int(args.max_bitrate * 1_000_000)
    codec = {"h264": "libx264", "hevc": "libx265"}[args.vcodec]
    cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-framerate", f"{args.fps:g}", "-i", "-",
           "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
           "-c:v", codec, "-preset", args.preset, "-crf", str(args.crf),
           "-maxrate", str(rate), "-bufsize", str(rate * 2),
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    cmd += ["-tag:v", "hvc1"] if args.vcodec == "hevc" else []
    start = _photo_time_utc(photos[0])
    if start:
        cmd += ["-metadata", "creation_time=" + start.strftime("%Y-%m-%dT%H:%M:%S.000000Z")]
    cmd += ["-an", "-movflags", "+faststart", str(out)]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    jobs = max(1, os.cpu_count() or 2)
    prev = b""
    try:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            window: deque = deque()
            it = iter(photos)
            for p in it:  # заранее читаем несколько фото, но не больше, чтобы не занять всю память
                window.append(pool.submit(frame, p))
                if len(window) >= jobs * 2:
                    break
            done = 0
            while window:
                data = window.popleft().result()
                nxt = next(it, None)
                if nxt is not None:
                    window.append(pool.submit(frame, nxt))
                data = data or prev
                if data:
                    proc.stdin.write(data)
                    prev = data
                done += 1
                if done % 100 == 0 or done == n:
                    print(f"\r  кадров: {done}/{n}", end="", flush=True)
        print()
        proc.stdin.close()
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        out.unlink(missing_ok=True)
        print("\nПрервано", file=sys.stderr)
        return 130
    err = proc.stderr.read().decode("utf-8", errors="replace").strip()
    if proc.wait() != 0:
        out.unlink(missing_ok=True)
        print(f"Ошибка ffmpeg: {err}", file=sys.stderr)
        return 1
    if start:
        # Дата файла = время окончания съёмки (так avtokino сверяет её с creation_time).
        end = start + timedelta(seconds=n / args.fps)
        os.utime(out, (end.timestamp(), end.timestamp()))
    size = out.stat().st_size
    print(f"Готово: {out.name}, {probe_duration(args.ffprobe, out):.1f} с, {size / 1_048_576:.1f} МБ")
    return 0


# --- rotate180 ----------------------------------------------------------------

ROTATE_SUFFIX = "_rotate180"
_MP4_LIKE = (".mp4", ".mov", ".m4v", ".3gp")


def _expand_masks(masks: list[str]) -> list[Path]:
    """Маски раскрываются здесь, потому что cmd.exe в Windows их не раскрывает."""
    import glob
    found: dict[Path, None] = {}
    for m in masks:
        hits = glob.glob(m, recursive=True) if glob.has_magic(m) else [m]
        if not hits:
            print(f"Нет файлов по маске: {m}", file=sys.stderr)
        for h in sorted(hits):
            p = Path(h)
            if p.is_file():
                found[p.resolve()] = None
    return list(found)


def rotate180(args) -> int:
    from datetime import timezone

    from .config import VIDEO_EXTS
    from .dating import avchd_date
    from .probe import ffprobe_json

    files = [p for p in _expand_masks(args.masks)
             if p.suffix.lower() in VIDEO_EXTS and not p.stem.endswith(ROTATE_SUFFIX)]
    if not files:
        print("Видеофайлов не найдено", file=sys.stderr)
        return 2
    errors = 0
    for src in files:
        ext = src.suffix if src.suffix.lower() in _MP4_LIKE else ".mp4"
        out = src.with_name(f"{src.stem}{ROTATE_SUFFIX}{ext}")
        if out.exists() and not args.force:
            print(f"{src.name}: пропуск, уже есть {out.name} (перезаписать: --force)")
            continue
        try:
            info = ffprobe_json(args.ffprobe, src)
        except Exception as e:  # noqa: BLE001
            print(f"{src.name}: не удалось прочитать ({e})", file=sys.stderr)
            errors += 1
            continue
        video = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"
                      and not st.get("disposition", {}).get("attached_pic")), None)
        if video is None:
            print(f"{src.name}: нет видеопотока", file=sys.stderr)
            errors += 1
            continue
        current = 0.0  # поворот в метаданных, как его показывает ffprobe (по часовой = минус)
        for sd in video.get("side_data_list", []) or []:
            if "rotation" in sd:
                current = float(sd["rotation"])
        if not current and video.get("tags", {}).get("rotate"):
            current = -float(video["tags"]["rotate"])
        new = (180 - current) % 360  # -display_rotation задаёт поворот против часовой стрелки

        cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
               "-display_rotation:v:0", f"{new:g}", "-i", str(src),
               "-map", "0:v", "-map", "0:a?", "-c", "copy", "-map_metadata", "0",
               "-movflags", "use_metadata_tags"]
        note = ""
        if ext != src.suffix:
            # В MPEG-TS/AVI/MKV нет пометки поворота, поэтому без перекодировки только смена контейнера на MP4.
            note = f", контейнер {src.suffix} → MP4"
            when = None
            if src.suffix.lower() in (".mts", ".m2ts"):
                ev = avchd_date(src)
                if ev and ev.value.tzinfo:
                    when = ev.value.astimezone(timezone.utc)
            if when is None and not info.get("format", {}).get("tags", {}).get("creation_time"):
                st = src.stat()
                when = datetime_from_ts(getattr(st, "st_birthtime", None) or st.st_mtime)
            if when is not None:
                cmd += ["-metadata", "creation_time=" + when.strftime("%Y-%m-%dT%H:%M:%S.000000Z")]
        # Потоки данных (телеметрия GoPro, таймкод) пробуем сохранить; если MP4 их не принимает, без них.
        proc = subprocess.run(cmd + ["-map", "0:d?", str(out)], capture_output=True)
        if proc.returncode != 0:
            proc = subprocess.run(cmd + [str(out)], capture_output=True)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            print(f"{src.name}: ошибка ffmpeg: {err}", file=sys.stderr)
            out.unlink(missing_ok=True)
            errors += 1
            continue
        _copy_times(src, out)
        print(f"{src.name} → {out.name}{note}")
    return 1 if errors else 0


def datetime_from_ts(ts: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# --- sync ---------------------------------------------------------------------

SYNC_RATE = 16000      # Гц, звук для точной сверки
COARSE_RATE = 2000     # Гц, звук для грубого поиска по всей длине


def _load_audio(ffmpeg: str, path: Path, max_len: float | None):
    import numpy as np
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path),
           "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SYNC_RATE)]
    if max_len:
        cmd += ["-t", f"{max_len:g}"]
    cmd += ["-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", errors="replace").strip() or "нет звуковой дорожки"
        raise RuntimeError(err.splitlines()[-1])
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _gcc_full(a, b, rate: int, lo: float, hi: float):
    """GCC-PHAT по всей длине: (корреляция, задержки t_A − t_B в отсчётах) для всех возможных сдвигов."""
    import numpy as np
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    X = np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n))
    freqs = np.fft.rfftfreq(n, 1 / rate)
    X[(freqs < lo) | (freqs > hi)] = 0
    X /= np.abs(X) + 1e-12
    cc = np.fft.irfft(X, n)
    del X
    cc = np.concatenate([cc[n - (len(b) - 1):], cc[:len(a)]])
    lags = np.arange(len(cc)) - (len(b) - 1)
    return cc, lags


def _gcc_phat(a, b, max_lag: int):
    """Точная задержка: обобщённая взаимная корреляция с фазовым преобразованием (GCC-PHAT)."""
    import numpy as np
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    X = np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n))
    freqs = np.fft.rfftfreq(n, 1 / SYNC_RATE)
    X[(freqs < 100) | (freqs > 6000)] = 0  # гул и шипение только мешают
    cc = np.fft.irfft(X / (np.abs(X) + 1e-12), n)
    cc = np.concatenate([cc[-max_lag:], cc[:max_lag + 1]])
    k = int(np.argmax(cc))
    shift = 0.0
    if 0 < k < len(cc) - 1:  # параболическая интерполяция между отсчётами
        y0, y1, y2 = cc[k - 1], cc[k], cc[k + 1]
        den = y0 - 2 * y1 + y2
        shift = 0.5 * (y0 - y2) / den if den else 0.0
    side = np.delete(cc, range(max(0, k - 40), min(len(cc), k + 41)))
    quality = float(cc[k] / (np.abs(side).max() + 1e-12)) if len(side) else 0.0
    return (k - max_lag + shift) / SYNC_RATE, quality


def _video_start(ffprobe: str, path: Path):
    """(время начала съёмки по метаданным как Evidence, fps, длительность)."""
    from .dating import check_utc, fs_date, name_date, video_meta
    from .probe import probe_video
    info = probe_video(ffprobe, path)
    fs = fs_date(path)
    meta, vote = check_utc(video_meta(info), fs, info.duration)
    return info, meta, name_date(path), fs, vote


@dataclass
class LagResult:
    lag: float          # начало B − начало A, с (= на сколько позже тот же звук в A)
    reliable: bool
    doubtful: bool
    text: str
    drift: tuple | None


def find_lag(a, b, lo: float | None = None, hi: float | None = None) -> LagResult | None:
    """Сдвиг между звуком a и b (оба SYNC_RATE, моно). lo/hi ограничивают искомый сдвиг, с."""
    import numpy as np
    # 1) грубо: B режется на куски по 30 с, каждый ищется во всём A (GCC-PHAT на 2 кГц),
    #    куски голосуют за сдвиг. Так не мешают ни уход часов камер, ни участки без общего звука.
    dec = SYNC_RATE // COARSE_RATE
    ca = a[:len(a) // dec * dec].astype(np.float64).reshape(-1, dec).mean(axis=1)
    cb = b[:len(b) // dec * dec].astype(np.float64).reshape(-1, dec).mean(axis=1)
    chunk = 30 * COARSE_RATE
    starts = list(range(0, max(1, len(cb) - chunk // 2), chunk)) or [0]
    n = 1 << int(np.ceil(np.log2(len(ca) + chunk)))
    freqs = np.fft.rfftfreq(n, 1 / COARSE_RATE)
    band = (freqs >= 100) & (freqs <= 900)
    FA = np.fft.rfft(ca, n)
    votes: dict[int, list] = {}
    for st in starts:
        piece = cb[st:st + chunk]
        if len(piece) < 5 * COARSE_RATE or piece.std() < 1e-5:
            continue
        X = FA * np.conj(np.fft.rfft(piece, n))
        X[~band] = 0
        cc = np.fft.irfft(X / (np.abs(X) + 1e-12), n)
        # задержка k: piece[t] ≈ ca[t + k], k от −len(piece)+1 до len(ca)−1
        cc = np.concatenate([cc[n - (len(piece) - 1):], cc[:len(ca)]])
        lags = np.arange(len(cc)) - (len(piece) - 1) - st  # t_A − t_B
        if lo is not None or hi is not None:
            ok = np.ones(len(cc), bool)
            if lo is not None:
                ok &= lags >= lo * COARSE_RATE
            if hi is not None:
                ok &= lags <= hi * COARSE_RATE
            if not ok.any():
                continue
            cc = np.where(ok, cc, -np.inf)
        k = int(np.argmax(cc))
        side = np.abs(np.delete(cc, range(max(0, k - 20), k + 21)))
        side = side[np.isfinite(side)]
        q = float(cc[k] / (np.percentile(side, 99.99) + 1e-12)) if len(side) else 0.0
        bin_ = int(round(lags[k] / (0.1 * COARSE_RATE)))  # корзины по 0,1 с
        votes.setdefault(bin_, []).append((q, int(lags[k])))
    used = sum(len(v) for v in votes.values())
    if not used:
        return None
    # соседние корзины объединяем: сдвиг на границе 0,1 с не должен делить голоса
    best_bin = max(votes, key=lambda k: sum(q for k2 in (k - 1, k, k + 1) for q, _ in votes.get(k2, [])))
    group = [x for k2 in (best_bin - 1, best_bin, best_bin + 1) for x in votes.get(k2, [])]
    agree = len(group)
    coarse = int(np.median([lag for _, lag in group])) * dec

    # 2) точно: GCC-PHAT ±50 мс вокруг грубой оценки на кусках общего участка по 20 с
    start_a, start_b = max(0, coarse), max(0, -coarse)
    total = min(len(a) - start_a, len(b) - start_b)
    if total < 2 * SYNC_RATE:
        return None

    def fine_at(pos: int, length: int):
        seg_a = a[start_a + pos:start_a + pos + length].astype(np.float64)
        seg_b = b[start_b + pos:start_b + pos + length].astype(np.float64)
        f, q = _gcc_phat(seg_a, seg_b, int(0.05 * SYNC_RATE))
        return coarse / SYNC_RATE + f, q

    length = min(total, 60 * SYNC_RATE)
    lag, fine_q = fine_at((total - length) // 2, length)   # середина общего участка
    drift = None
    if total >= 120 * SYNC_RATE:  # начало и конец: уход часов одной камеры относительно другой
        win = 20 * SYNC_RATE
        (l1, q1), (l2, q2) = fine_at(5 * SYNC_RATE, win), fine_at(total - win - 5 * SYNC_RATE, win)
        if min(q1, q2) >= 1.2 and abs(l2 - l1) < 0.05:
            drift = (l1, l2, (total - win - 10 * SYNC_RATE) / SYNC_RATE)
    # надёжность: сколько 30-секундных кусков B указали на тот же сдвиг, и чёткость точного пика
    if used >= 3:
        reliable = agree >= max(2, 0.3 * used)
        doubtful = not reliable and agree >= 2
    else:
        reliable, doubtful = fine_q >= 3, fine_q >= 1.5
    quality_text = (f"кусков по 30 с, согласных со сдвигом: {agree} из {used}; " if used >= 3 else "") + \
        f"чёткость пика {fine_q:.1f}"

    return LagResult(lag, reliable, doubtful, quality_text, drift)


def sync(args) -> int:
    try:
        import numpy as np
    except ImportError:
        print("Для команды sync нужен numpy: pip install numpy", file=sys.stderr)
        return 2
    from datetime import timedelta

    from .dating import estimate_offset, resolve

    paths = [Path(args.a).resolve(), Path(args.b).resolve()]
    for p in paths:
        if not p.is_file():
            print(f"Файл не найден: {p}", file=sys.stderr)
            return 2
    try:
        audio = [_load_audio(args.ffmpeg, p, args.max_duration) for p in paths]
    except RuntimeError as e:
        print(f"Не удалось прочитать звук: {e}", file=sys.stderr)
        return 1
    a, b = audio
    if min(len(a), len(b)) < SYNC_RATE * 2:
        print("Слишком короткий звук для сверки (нужно хотя бы 2 с)", file=sys.stderr)
        return 1

    r = find_lag(a, b, -args.max_offset if args.max_offset else None, args.max_offset)
    if r is None:
        print("Не нашлось общего звука для сверки", file=sys.stderr)
        return 3
    lag, reliable, doubtful, quality_text, drift = (r.lag, r.reliable, r.doubtful, r.text, r.drift)

    infos = [_video_start(args.ffprobe, p) for p in paths]
    names = [p.name for p in paths]
    print(f"A: {names[0]}")
    print(f"B: {names[1]}")
    print()
    if lag >= 0:
        print(f"A начал запись на {lag:.3f} с раньше B: один и тот же звук в A на {lag:.3f} с дальше от начала.")
    else:
        print(f"B начал запись на {-lag:.3f} с раньше A: один и тот же звук в B на {-lag:.3f} с дальше от начала.")
    print(f"Момент 0:00.000 в A соответствует {_fmt_t(-lag)} в B.")
    for name, (info, *_rest) in zip("AB", infos):
        fps = float(info.fps) if info.fps else 0
        if fps:
            print(f"  в кадрах {name} ({fps:.3f} кадр/с): {abs(lag) * fps:.1f}")
    verdict = ("надёжно" if reliable else "сомнительно" if doubtful else
               "ненадёжно: похоже, звук роликов не перекрывается")
    print(f"Уверенность: {verdict} ({quality_text})")
    if drift:
        l1, l2, span = drift
        if abs(l2 - l1) >= 0.01:
            print(f"Часы камер немного расходятся: в начале общего участка сдвиг {l1:.3f} с, "
                  f"через {span / 60:.0f} мин {l2:.3f} с ({(l2 - l1) / span * 1e6:+.0f} мкс/с). Выше — сдвиг в середине.")

    # сравнение с метаданными: какую поправку дать в --shift, чтобы avtokino поставил их точно
    est = estimate_offset([(m, n) for _, m, n, _, _ in infos], [v for *_, v in infos if v is not None])
    off = est[0] if est else (datetime_from_ts(0).astimezone().utcoffset() or timedelta(0))
    starts = [resolve(m, n, fs, off) for _, m, n, fs, _ in infos]
    meta_diff = (starts[1].when - starts[0].when).total_seconds()
    real_diff = lag                          # начало B − начало A по звуку
    print()
    print(f"По метаданным: A {starts[0].when:%Y-%m-%d %H:%M:%S} [{starts[0].source}]")
    print(f"               B {starts[1].when:%Y-%m-%d %H:%M:%S} [{starts[1].source}]")
    fix = real_diff - meta_diff
    print(f"По метаданным B начат на {meta_diff:+.2f} с относительно A, по звуку на {real_diff:+.3f} с.")
    if not (reliable or doubtful):
        print("Сдвиг по звуку ненадёжен, поправку для --shift не предлагаю.")
    elif abs(fix) >= 0.5:
        from .dating import fmt_delta
        print(f"Часы B расходятся с часами A на {fmt_delta(timedelta(seconds=-fix))}.")
        print(f"Чтобы avtokino считал время B по часам A: --shift \"{names[1]}={fix:+.2f}\"")
    else:
        print("Часы камер расходятся меньше чем на 0,5 с, поправка не нужна.")
    return 0 if reliable or doubtful else 3


def _fmt_t(sec: float) -> str:
    sign = "−" if sec < 0 else ""
    sec = abs(sec)
    m, s_ = divmod(sec, 60)
    return f"{sign}{int(m)}:{s_:06.3f}"


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

    tl = sub.add_parser("timelapse", help="собрать ролик из фото, одно фото = один кадр",
                        description="Собирает ролик из всех фото каталога (порядок по имени файла, "
                                    "IMG_9 раньше IMG_10), одно фото = один кадр. Ролик <каталог>.mp4 "
                                    "сохраняется в том же каталоге (если есть, то <каталог>_v2.mp4 и т. д.).")
    tl.add_argument("dir", help="каталог с фотографиями")
    tl.add_argument("--fps", type=float, default=15.0, help="частота кадров (15)")
    tl.add_argument("--resolution", type=_parse_resolution, metavar="ШxВ",
                    help="разрешение, например 1920x1080 (по умолчанию как у первой фотографии); "
                         "фото другого формата вписываются с чёрными полями")
    tl.add_argument("--max-bitrate", type=float, default=15.0, metavar="МБИТ",
                    help="максимальный видеопоток, Мбит/с (15)")
    tl.add_argument("--crf", type=int, default=20, help="качество x264/x265, меньше = лучше (20)")
    tl.add_argument("--preset", default="medium", help="пресет кодировщика (medium); быстрее: veryfast")
    tl.add_argument("--vcodec", choices=["h264", "hevc"], default="h264", help="видеокодек (h264)")
    tl.set_defaults(func=timelapse)

    r = sub.add_parser("rotate180", help="перевернуть видео на 180° без перекодировки",
                       description="Переворачивает видео на 180° без перекодировки: меняется только пометка "
                                   "поворота в контейнере, метаданные и даты файла сохраняются. Результат: "
                                   "<имя>_rotate180.<расширение> рядом с исходником. Файлы .MTS/.AVI/.MKV "
                                   "сохраняются в MP4, потому что в их контейнерах пометки поворота нет.")
    r.add_argument("masks", nargs="+", metavar="МАСКА", help="файлы или маски, например *.MP4 или E:\\video\\GOPR*.MP4")
    r.add_argument("--force", action="store_true", help="перезаписать существующий результат")
    r.set_defaults(func=rotate180)

    sy = sub.add_parser("sync", help="рассинхронизация двух роликов с разных камер по звуку",
                        description="Находит, на сколько сдвинуты друг относительно друга два ролика, снятые "
                                    "одновременно разными камерами: сравнивает их звук. Точность лучше 0,01 с. "
                                    "Также сравнивает результат с датами съёмки и подсказывает --shift для avtokino.")
    sy.add_argument("a", help="первый ролик (A)")
    sy.add_argument("b", help="второй ролик (B)")
    sy.add_argument("--max-offset", type=float, metavar="СЕК",
                    help="искать сдвиг не больше СЕК секунд (по умолчанию любой)")
    sy.add_argument("--max-duration", type=float, metavar="СЕК",
                    help="читать только первые СЕК секунд звука каждого ролика (для очень длинных файлов)")
    sy.set_defaults(func=sync)

    from .multicam import add_parser as _add_multicam
    _add_multicam(sub, _parse_resolution)

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
