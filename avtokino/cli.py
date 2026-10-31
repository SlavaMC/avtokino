"""Точка входа: python -m avtokino <рабочий каталог> [параметры]."""
from __future__ import annotations

import argparse
import fnmatch
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from . import __version__
from .assemble import build_music, concat_audio, mix_audio, mux
from .config import IGNORED_NAMES, PHOTO_EXTS, VIDEO_EXTS, Settings
from .dating import check_utc, fmt_delta, parse_time_fix, estimate_offset, fs_date, name_date, photo_meta, resolve, video_meta
from .naming import choose_output, output_pattern, parse_dir_name
from .planner import Item, build_plan, choose_base, fps_str, gopro_trim
from .probe import HEIF_SUPPORTED, probe_photo, probe_video
from .render import RenderContext, has_filter, make_caption, render_segment

log = logging.getLogger("avtokino")


# --- разбор аргументов ------------------------------------------------------

def _bitrate(value: str) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", value.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"битрейт: {value}")
    mult = {"": 1, "k": 1000, "m": 1_000_000}[m[2].lower()]
    return int(float(m[1]) * mult)


def _resolution(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)[xX:](\d+)", value.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"разрешение: {value}, нужно, например, 1920x1080")
    w, h = int(m[1]), int(m[2])
    if w * 9 != h * 16:
        raise argparse.ArgumentTypeError("разрешение должно быть 16:9")
    return w, h


def _time_fix(value: str):
    try:
        return parse_time_fix(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def _fps(value: str) -> Fraction:
    return Fraction(value).limit_denominator(1001)


def parse_args(argv: list[str] | None = None) -> Settings:
    p = argparse.ArgumentParser(prog="avtokino",
                                description="Собирает видеоролик из фото и видео рабочего каталога.")
    p.add_argument("workdir", type=Path, help="каталог события, например 20240615_Wedding")
    g = p.add_argument_group("видео")
    g.add_argument("--resolution", type=_resolution, help="базовое разрешение 16:9, например 1920x1080")
    g.add_argument("--fps", type=_fps, help="базовая частота кадров, например 25 или 30000/1001")
    g.add_argument("--vcodec", choices=["h264", "hevc"], help="базовый кодек")
    g.add_argument("--vbitrate", type=_bitrate,
                   help="базовый битрейт (потолок), например 12M; без него только по качеству CRF")
    g.add_argument("--crf", type=int, help="качество кодирования (h264: 20, hevc: 22 по умолчанию)")
    g.add_argument("--preset", default="medium", help="скорость кодировщика x264/x265 (по умолчанию medium)")
    g.add_argument("--safe", action="store_true", help="перекодировать всё, без копирования")
    g = p.add_argument_group("звук")
    g.add_argument("--acodec", default="aac", help="аудиокодек (aac)")
    g.add_argument("--abitrate", default="192k", help="аудиобитрейт (192k)")
    g.add_argument("--arate", type=int, default=48000, help="частота дискретизации (48000)")
    g.add_argument("--achannels", type=int, default=2, choices=[1, 2], help="число каналов (2)")
    g.add_argument("--volume", type=float, default=1.0, help="громкость основного звука (1.0)")
    g.add_argument("--music", type=Path, help="каталог с фоновой музыкой")
    g.add_argument("--music-volume", type=float, default=0.3, help="громкость музыки (0.3)")
    g.add_argument("--music-crossfade", type=float, default=3.0, help="перекрытие треков, с (3)")
    g.add_argument("--music-fadeout", type=float, default=5.0, help="затухание музыки в конце, с (5)")
    g.add_argument("--no-ducking", action="store_true", help="не приглушать музыку под живым звуком")
    g = p.add_argument_group("фото и монтаж")
    g.add_argument("--photo-duration", type=float, default=5.0, help="длительность фото, с (5)")
    g.add_argument("--photo-burst-duration", type=float, default=3.0, help="длительность фото в серии, с (3)")
    g.add_argument("--photo-burst-threshold", type=int, default=5,
                   help="серия = больше стольких фото подряд (5)")
    g.add_argument("--photo-zoom", type=float, default=1.05, help="зум фото за кадр (1.05 = 5%%)")
    g.add_argument("--exclude", action="append", metavar="КАТАЛОГ",
                   help="не обрабатывать каталог: имя на любом уровне (Черновики), путь от рабочего "
                        "каталога (Mobile/Screenshots) или маска (*_old); можно указать несколько раз")
    g.add_argument("--min-clip", type=float, default=5.0,
                   help="пропускать видео короче, с (5); для GoPro считается после обрезки")
    g.add_argument("--gopro-trim", type=float, nargs="?", const=3.0, default=0.0, metavar="СЕК",
                   help="отрезать конец у роликов GoPro (GOPR*, GP*, GH*, GX*); без числа — 3 с")
    g.add_argument("--photo-fit", choices=["contain", "cover"], default="contain",
                   help="фото: contain — целиком с чёрными полями, cover — на весь кадр с обрезкой "
                        "(вертикальные фото всегда целиком)")
    g.add_argument("--date-tolerance", type=float, default=6.0, metavar="ЧАСЫ",
                   help="запас вокруг периода из имени каталога; файлы за его пределами идут в конец (6)")
    g.add_argument("--time-fix", action="append", type=_time_fix, metavar="'БЫЛО -> СТАЛО'",
                   help="поправка сбитых часов: \"2016-01-04 23:02:42 -> 2025-06-20 09:48:00\"; "
                        "применяется к файлам вне периода события, если с ней они в него попадают; "
                        "можно указать несколько, пробуются по порядку")
    g.add_argument("--tz", help="часовой пояс съёмки (Europe/Moscow или +03:00), если не определился сам")
    g.add_argument("--transition", choices=["none", "fade"], default="none",
                   help="переходы: fade = через чёрное (требует перекодировки всех клипов)")
    g.add_argument("--transition-duration", type=float, default=0.5, help="длительность перехода, с")
    g.add_argument("--title", action="store_true", help="титр с названием и датой события в начале")
    g.add_argument("--title-text", action="append", metavar="СТРОКА",
                   help="свой текст титра; каждая --title-text — отдельная строка, первая крупнее "
                        "(включает титр)")
    g.add_argument("--title-duration", type=float, default=4.0, metavar="СЕК", help="длительность титра, с (4)")
    g.add_argument("--caption", action="store_true",
                   help="подпись (название и дата) в правом нижнем углу в начале ролика")
    g.add_argument("--caption-name", metavar="ТЕКСТ", help="первая строка подписи (по умолчанию из имени каталога)")
    g.add_argument("--caption-date", metavar="ТЕКСТ", help="вторая строка подписи (по умолчанию из даты каталога)")
    g.add_argument("--caption-duration", type=float, default=10.0, metavar="СЕК",
                   help="сколько секунд от начала показывать подпись (10)")
    g.add_argument("--caption-font", metavar="ФАЙЛ", help="шрифт подписи (.ttf), например Roboto-Regular.ttf")
    g = p.add_argument_group("работа")
    g.add_argument("--jobs", type=int, default=2, help="параллельных задач рендера (2)")
    g.add_argument("--temp-dir", type=Path, help="каталог для временных файлов (по умолчанию в рабочем)")
    g.add_argument("--keep-temp", action="store_true", help="не удалять временные файлы")
    g.add_argument("--dry-run", action="store_true", help="только план и лог, без рендера")
    g.add_argument("--ffmpeg", default="ffmpeg", help="путь к ffmpeg")
    g.add_argument("--ffprobe", default="ffprobe", help="путь к ffprobe")
    g.add_argument("-v", "--verbose", action="store_true", help="подробный вывод в консоль")
    p.add_argument("--version", action="version", version=f"avtokino {__version__}")
    a = p.parse_args(argv)
    return Settings(
        workdir=a.workdir.resolve(), resolution=a.resolution, fps=a.fps, vcodec=a.vcodec,
        vbitrate=a.vbitrate, crf=a.crf, preset=a.preset, acodec=a.acodec, abitrate=a.abitrate,
        arate=a.arate, achannels=a.achannels, volume=a.volume,
        music_dir=a.music.resolve() if a.music else None, music_volume=a.music_volume,
        music_crossfade=a.music_crossfade, music_fadeout=a.music_fadeout, ducking=not a.no_ducking,
        photo_duration=a.photo_duration, photo_burst_duration=a.photo_burst_duration,
        photo_burst_threshold=a.photo_burst_threshold, photo_zoom=a.photo_zoom, min_clip=a.min_clip, exclude=a.exclude or [],
        gopro_trim=a.gopro_trim, photo_fit=a.photo_fit,
        caption=a.caption or bool(a.caption_name or a.caption_date), caption_name=a.caption_name,
        caption_date=a.caption_date, caption_duration=a.caption_duration, caption_font=a.caption_font,
        tz=a.tz, date_tolerance=a.date_tolerance, time_fixes=a.time_fix or [],
        transition=a.transition, transition_duration=a.transition_duration,
        title=a.title or bool(a.title_text), title_text=a.title_text,
        title_duration=a.title_duration, jobs=max(1, a.jobs), temp_dir=a.temp_dir,
        keep_temp=a.keep_temp, dry_run=a.dry_run, safe=a.safe, ffmpeg=a.ffmpeg, ffprobe=a.ffprobe,
        verbose=a.verbose,
    )


# --- вспомогательное --------------------------------------------------------

def setup_logging(log_path: Path, verbose: bool) -> None:
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(fh)
    log.addHandler(ch)


def _tz_offset(spec: str | None, sample: datetime) -> tuple[timedelta, str]:
    if spec:
        m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", spec)
        if m:
            sign = 1 if m[1] == "+" else -1
            return sign * timedelta(hours=int(m[2]), minutes=int(m[3] or 0)), f"из командной строки ({spec})"
        from zoneinfo import ZoneInfo
        off = sample.replace(tzinfo=ZoneInfo(spec)).utcoffset()
        return off, f"из командной строки ({spec})"
    off = sample.replace(tzinfo=timezone.utc).astimezone().utcoffset()
    return off, "часовой пояс этого компьютера"


def _fmt_offset(off: timedelta) -> str:
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _fmt_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _fmt_dur(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def is_excluded(rel: str, patterns: list[str]) -> bool:
    """rel — путь каталога от рабочего, через «/». Шаблон без «/» сравнивается с именем каталога,
    с «/» — с путём целиком; регистр не важен, допускаются маски * и ?."""
    rel = rel.casefold()
    name = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        pat = pat.replace("\\", "/").strip("/").casefold()
        if fnmatch.fnmatchcase(rel if "/" in pat else name, pat):
            return True
    return False


def scan(s: Settings, out_re: re.Pattern, skip_dirs: list[Path]) -> tuple[list[Path], list[Path]]:
    videos, photos = [], []
    for root, dirs, files in os.walk(s.workdir):
        rootp = Path(root)
        kept = []
        for d in sorted(dirs):
            if d.startswith(".") or any((rootp / d).resolve() == sd for sd in skip_dirs):
                continue
            rel = (rootp / d).relative_to(s.workdir).as_posix()
            if s.exclude and is_excluded(rel, s.exclude):
                log.info("Пропуск каталога (--exclude): %s", rel)
                continue
            kept.append(d)
        dirs[:] = kept
        for f in sorted(files):
            p = rootp / f
            ext = p.suffix.lower()
            if f.lower() in IGNORED_NAMES or f.startswith(".") or ext not in VIDEO_EXTS | PHOTO_EXTS:
                continue
            if out_re.match(p.stem):
                log.info("Пропуск (ранее собранный ролик): %s", p.relative_to(s.workdir))
                continue
            if ext in VIDEO_EXTS:
                videos.append(p)
            elif ext in PHOTO_EXTS:
                if ext in (".heic", ".heif") and not HEIF_SUPPORTED:
                    log.warning("Пропуск %s: для HEIC установите пакет pillow-heif", p.name)
                    continue
                photos.append(p)
    return videos, photos


# --- основной сценарий ------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    s = parse_args(argv)
    if not s.workdir.is_dir():
        print(f"Каталог не найден: {s.workdir}", file=sys.stderr)
        return 2
    for tool in (s.ffmpeg, s.ffprobe):
        if shutil.which(tool) is None and not Path(tool).is_file():
            print(f"Не найден {tool}. Установите ffmpeg или укажите путь через --ffmpeg/--ffprobe.",
                  file=sys.stderr)
            return 2

    output = choose_output(s.workdir)
    setup_logging(output.with_suffix(".log"), s.verbose)
    started = time.monotonic()
    event = parse_dir_name(s.workdir.name)

    log.info("=== avtokino %s, %s ===", __version__, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Рабочий каталог: %s", s.workdir)
    log.info("Событие: «%s», %s", event.name or "—", event.describe())
    log.info("Итоговый ролик: %s", output.name)
    log.info("Лог: %s", output.with_suffix(".log").name)

    tmp_parent = s.temp_dir or s.workdir
    tmp_parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".avtokino_tmp_", dir=tmp_parent))
    skip = [tmp.resolve()] + ([s.music_dir] if s.music_dir else [])
    try:
        return _run(s, event, output, tmp, skip, started)
    except KeyboardInterrupt:
        log.error("Прервано пользователем")
        return 130
    finally:
        if s.keep_temp:
            log.info("Временные файлы сохранены: %s", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def _run(s: Settings, event, output: Path, tmp: Path, skip: list[Path], started: float) -> int:
    video_paths, photo_paths = scan(s, output_pattern(s.workdir.name), skip)
    log.info("Найдено: видео %d, фото %d", len(video_paths), len(photo_paths))

    # анализ файлов
    raw: list[tuple[Path, str, object, object, object, object]] = []
    votes = []
    for p in video_paths:
        try:
            info = probe_video(s.ffprobe, p)
        except Exception as e:  # noqa: BLE001
            log.warning("Пропуск %s: не удалось прочитать (%s)", p.name, e)
            continue
        rel = p.relative_to(s.workdir).as_posix()
        if info is None:
            log.warning("Пропуск %s: нет видеопотока", rel)
            continue
        trim = gopro_trim(p, s)
        if info.duration - trim < s.min_clip:
            extra = f" после обрезки GoPro на {trim:g} с" if trim else ""
            log.info("Пропуск %s: короче %.0f с%s (%.1f с)", rel, s.min_clip, extra, info.duration - trim)
            continue
        fs = fs_date(p)
        meta, vote = check_utc(video_meta(info), fs, info.duration)
        if vote is not None:
            votes.append(vote)
        raw.append((p, "video", info, meta, name_date(p), fs))
    for p in photo_paths:
        try:
            info = probe_photo(p)
        except Exception as e:  # noqa: BLE001
            log.warning("Пропуск %s: не удалось открыть (%s)", p.name, e)
            continue
        raw.append((p, "photo", info, photo_meta(info), name_date(p), fs_date(p)))

    if not raw:
        log.error("Нет файлов для монтажа")
        return 1

    # часовой пояс
    est = estimate_offset([(r[3], r[4]) for r in raw], votes)
    if est and not s.tz:
        offset, n_votes = est
        tz_note = f"определён автоматически, голосов: {n_votes}"
    else:
        sample = next((r[3] or r[4] or r[5]).value for r in raw)
        offset, tz_note = _tz_offset(s.tz, sample.replace(tzinfo=None))
    log.info("Часовой пояс съёмки: %s (%s)", _fmt_offset(offset), tz_note)

    items = []
    for p, kind, info, meta, name, fs in raw:
        dr = resolve(meta, name, fs, offset)
        items.append(Item(p, p.relative_to(s.workdir).as_posix(), kind, info, dr.when, dr.source))
    # Файлы с датой вне периода события идут в конец, между собой тоже по времени.
    period = event.period(timedelta(hours=s.date_tolerance))
    outside = set()
    if period:
        for k, delta in enumerate(s.time_fixes or [], 1):
            log.info("Поправка часов №%d: %s", k, fmt_delta(delta))
        for it in items:
            if period[0] <= it.when < period[1]:
                continue
            for k, delta in enumerate(s.time_fixes or [], 1):
                fixed = it.when + delta
                if period[0] <= fixed < period[1]:
                    log.info("  %s: %s → %s (поправка №%d)", it.rel, it.when.strftime("%Y-%m-%d %H:%M:%S"),
                             fixed.strftime("%Y-%m-%d %H:%M:%S"), k)
                    it.when = fixed
                    it.date_source += f", исправлено поправкой №{k}"
                    break
            else:
                outside.add(it.rel)
    elif s.time_fixes:
        log.warning("Поправки часов не применены: в имени каталога нет периода события")
    items.sort(key=lambda it: (it.rel in outside, it.when, it.rel))

    base = choose_base([it.info for it in items if it.kind == "video"], s)
    log.info("")
    log.info("--- Базовые параметры ---")
    log.info("Разрешение:      %s", base.resolution)
    log.info("Соотношение:     16:9 (несовпадающий контент вписывается с чёрными полями)")
    log.info("Видеокодек:      %s (CRF %d, пресет %s)", base.vcodec, base.crf, s.preset)
    log.info("Частота кадров:  %s", fps_str(base.fps))
    log.info("Видеобитрейт:    %s", f"≤ {base.vbitrate // 1000} кбит/с" if base.vbitrate
             else "не ограничен (режим качества CRF)")
    log.info("Аудио:           %s %s, %d Гц, %d кан.", s.acodec, s.abitrate, s.arate, s.achannels)
    log.info("Громкость:       основная %.2f%s", s.volume,
             f", музыка {s.music_volume:.2f} из {s.music_dir}" if s.music_dir else ", без музыки")
    log.info("Основание:       %s", base.source)
    log.info("Фото:            %s", "горизонтальные на весь кадр с обрезкой, вертикальные целиком"
             if s.photo_fit == "cover" else "целиком, с полями")
    if s.gopro_trim > 0:
        log.info("GoPro:           обрезка последних %g с", s.gopro_trim)

    log.info("")
    log.info("--- Хронология (%d файлов) ---", len(items))
    if period:
        log.info("Период события: %s – %s (с запасом %g ч)", period[0].strftime("%Y-%m-%d %H:%M"),
                 period[1].strftime("%Y-%m-%d %H:%M"), s.date_tolerance)
    else:
        log.info("Период события в имени каталога не указан, все файлы идут по времени")
    first_outside = next((it for it in items if it.rel in outside), None)
    for it in items:
        if it is first_outside:
            log.info("--- Вне периода события, в конец ролика (%d) ---", len(outside))
        log.info("%s  %-5s  %s  [%s]", it.when.strftime("%Y-%m-%d %H:%M:%S"),
                 "видео" if it.kind == "video" else "фото", it.rel, it.date_source)

    caption_lines = []
    if s.caption:
        caption_lines = [ln for ln in (s.caption_name if s.caption_name is not None else event.name,
                                       s.caption_date if s.caption_date is not None else event.caption_date())
                         if ln]
        log.info("Подпись:         %s (%g с, с %g с ролика%s)", " / ".join(caption_lines), s.caption_duration,
                 s.caption_start, ", после титра" if s.title else "")
    if s.title:
        log.info("Титр:            %s (%g с)", " / ".join(s.title_text or [event.name or s.workdir.name,
                                                                         event.title_date()]), s.title_duration)
    segments = build_plan(items, base, s)
    total = sum(seg.duration for seg in segments)
    abytes = _bitrate(s.abitrate) * total / 8
    size = sum(seg.est_bytes for seg in segments) + abytes
    copies = sum(1 for seg in segments if seg.kind == "copy")
    log.info("")
    log.info("Расчётная длительность: %s (%.1f с)", _fmt_dur(total), total)
    log.info("Примерный размер:       %s", _fmt_size(size))
    log.info("Видео без перекодировки: %d из %d", copies,
             sum(1 for seg in segments if seg.kind in ("copy", "transcode")))

    log.info("")
    log.info("--- План обработки ---")
    for seg in segments:
        name = seg.item.rel if seg.item else "титр"
        log.info("[%d] %s — %.2f с", seg.index + 1, name, seg.duration)
        for a in seg.actions:
            log.info("      видео: %s", a)
        log.info("      звук:  %s", seg.audio_note)

    if s.dry_run:
        log.info("")
        log.info("Режим --dry-run: рендер не выполнялся")
        return 0

    # рендер
    log.info("")
    log.info("--- Рендер (%d задач параллельно) ---", s.jobs)
    title_lines = s.title_text or [ln for ln in (event.name or s.workdir.name, event.title_date()) if ln]
    ctx = RenderContext(has_filter(s.ffmpeg, "zscale"), title_lines)
    if s.caption:
        ctx.caption, ctx.caption_box = make_caption(base, s, caption_lines)
        ctx.caption_path = tmp / "caption.png"
        ctx.caption.save(ctx.caption_path)
    failed = []
    with ThreadPoolExecutor(max_workers=s.jobs) as pool:
        futures = {pool.submit(render_segment, seg, base, s, tmp, ctx): seg
                   for seg in segments}
        done = 0
        for fut in as_completed(futures):
            seg = futures[fut]
            name = seg.item.rel if seg.item else "титр"
            done += 1
            try:
                fut.result()
                log.info("(%d/%d) готово: %s — %.2f с", done, len(segments), name, seg.real_duration)
            except Exception as e:  # noqa: BLE001
                failed.append(seg)
                log.error("(%d/%d) ОШИБКА: %s — %s; файл не войдёт в ролик", done, len(segments), name, e)
    segments = [seg for seg in segments if seg not in failed]
    if not segments:
        log.error("Ни один сегмент не собран")
        return 1

    real_total = sum(seg.real_duration for seg in segments)
    log.info("Звуковая дорожка...")
    main_wav = concat_audio(segments, s, tmp)
    music = build_music(real_total, s, tmp) if s.music_dir else None
    audio = mix_audio(main_wav, music, s, tmp)
    log.info("Сборка ролика...")
    part = tmp / ("final" + output.suffix)
    mux(segments, audio, base, s, tmp, part)
    shutil.move(str(part), output)

    log.info("")
    log.info("Готово: %s", output.name)
    log.info("Длительность: %s, размер: %s, время работы: %s", _fmt_dur(real_total),
             _fmt_size(output.stat().st_size), _fmt_dur(time.monotonic() - started))
    if failed:
        log.warning("Не вошли из-за ошибок: %s", ", ".join(seg.item.rel for seg in failed if seg.item))
    return 0 if not failed else 3
