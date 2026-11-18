"""Команда transcribe: расшифровка речи в видео (или аудио) в субтитры и текст с датой и временем.

Распознавание — модель Whisper через пакет faster-whisper, локально на процессоре, без интернета
(модель скачивается один раз при первом запуске). Звук извлекается ffmpeg.

Результат рядом с роликом:
  <имя>_transcribe.srt — субтитры (строки не длиннее ~80 знаков и ~7 секунд);
  <имя>_transcribe.txt — те же строки в виде «ГГГГ-ММ-ДД ЧЧ:ММ:СС: текст», где время —
                         дата съёмки ролика + смещение строки от начала ролика;
  с --embed ещё <имя>_transcribe.mp4 — копия ролика с дорожкой субтитров (без перекодировки).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SUFFIX = "_transcribe"
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
MAX_CHARS = 80
MAX_SECONDS = 7.0


@dataclass
class Line:
    start: float
    end: float
    text: str


# --- разбиение на строки ------------------------------------------------------------

def split_lines(segments) -> list[Line]:
    """Сегменты Whisper (со словами) -> строки субтитров: конец фразы, не длиннее MAX_CHARS
    знаков и MAX_SECONDS секунд."""
    lines: list[Line] = []
    for seg in segments:
        words = list(getattr(seg, "words", None) or [])
        if not words:
            text = seg.text.strip()
            if text:
                lines.append(Line(seg.start, seg.end, text))
            continue
        cur: list = []
        for w in words:
            if cur:
                text = "".join(x.word for x in cur).strip()
                too_long = len(text) + len(w.word) > MAX_CHARS or w.end - cur[0].start > MAX_SECONDS
                sentence_end = text[-1:] in ".!?…" and len(text) >= 25
                if sentence_end:
                    lines.append(Line(cur[0].start, cur[-1].end, text))
                    cur = []
                elif too_long:
                    # лучше переносить после запятой, если она не в самом начале строки
                    k = max((j for j, x in enumerate(cur[:-1]) if x.word.rstrip()[-1:] in ",;:—"
                             and len("".join(y.word for y in cur[:j + 1])) >= 20), default=None)
                    head = cur if k is None else cur[:k + 1]
                    lines.append(Line(head[0].start, head[-1].end, "".join(x.word for x in head).strip()))
                    cur = [] if k is None else cur[k + 1:]
            cur.append(w)
        if cur:
            text = "".join(x.word for x in cur).strip()
            if text:
                lines.append(Line(cur[0].start, cur[-1].end, text))
    return split_lines_fix(lines)


def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(lines: list[Line]) -> str:
    return "".join(f"{i}\n{_srt_time(ln.start)} --> {_srt_time(ln.end)}\n{ln.text}\n\n"
                   for i, ln in enumerate(lines, 1))


def to_dated(lines: list[Line], start: datetime) -> str:
    return "".join(f"{(start + timedelta(seconds=ln.start)):%Y-%m-%d %H:%M:%S}: {ln.text}\n" for ln in lines)


# --- дата съёмки ------------------------------------------------------------------------------

def shooting_start(path: Path, ffprobe: str, tz: str | None) -> tuple[datetime, str]:
    """Местное время начала записи и откуда оно взято (как в автосборщике)."""
    from .cli import _tz_offset
    from .dating import check_utc, estimate_offset, fs_date, name_date, resolve, video_meta
    from .probe import probe_video

    fs = fs_date(path)
    name = name_date(path)
    meta, votes = None, []
    duration = 0.0
    try:
        info = probe_video(ffprobe, path)
    except Exception:  # noqa: BLE001
        info = None
    if info is not None:
        duration = info.duration
        meta, vote = check_utc(video_meta(info), fs, info.duration)
        if vote is not None:
            votes.append(vote)
    est = estimate_offset([(meta, name)], votes)
    if est and not tz:
        offset = est[0]
    else:
        offset, _ = _tz_offset(tz, (meta or name or fs).value.replace(tzinfo=None))
    dr = resolve(meta, name, fs, offset)
    when, source = dr.when, dr.source
    # дата изменения файла — это конец записи
    if not meta and not name and duration:
        when -= timedelta(seconds=duration)
        source += " минус длительность"
    return when, source


# --- распознавание ------------------------------------------------------------------------------

def _audio(ffmpeg: str, path: Path):
    import numpy as np
    proc = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-vn",
                           "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError("нет звука или не удалось прочитать: " + " | ".join(err[-2:]))
    return np.frombuffer(proc.stdout, np.float32)


def _model(args):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("Для команды transcribe нужен пакет faster-whisper: pip install faster-whisper", file=sys.stderr)
        raise SystemExit(2)
    print(f"Модель {args.model} (при первом запуске скачивается, 0,5–3 ГБ)...")
    return WhisperModel(args.model, device="cpu", compute_type="int8",
                        cpu_threads=args.threads or (os.cpu_count() or 4))


def _fmt(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def transcribe(args) -> int:
    from .config import VIDEO_EXTS
    from .tools import _copy_times, _expand_masks

    started = time.monotonic()
    files = [p for p in _expand_masks(args.files)
             if p.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS and not p.stem.endswith(SUFFIX)]
    if not files:
        print("Видео или аудиофайлов не найдено", file=sys.stderr)
        return 2
    start_override = None
    if args.start_time:
        try:
            start_override = datetime.strptime(args.start_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            print("--start-time: нужен формат \"ГГГГ-ММ-ДД ЧЧ:ММ:СС\"", file=sys.stderr)
            return 2
    model = _model(args)
    failed = 0
    for i, path in enumerate(files, 1):
        t0 = time.monotonic()
        print(f"[{i}/{len(files)}] {path.name}")
        if start_override:
            start, source = start_override, "из командной строки"
        else:
            start, source = shooting_start(path, args.ffprobe, args.tz)
        print(f"  начало записи: {start:%Y-%m-%d %H:%M:%S} ({source})")
        try:
            audio = _audio(args.ffmpeg, path)
        except RuntimeError as e:
            print(f"  ПРОПУСК: {e}", file=sys.stderr)
            failed += 1
            continue
        dur = len(audio) / 16000
        segments, info = model.transcribe(
            audio, language=args.language, beam_size=args.beam, word_timestamps=True,
            vad_filter=not args.no_vad, condition_on_previous_text=False)
        lines = []
        for seg in segments:          # генератор: распознавание идёт по мере чтения
            lines.extend(split_lines([seg]))
            print(f"\r  распознано {_fmt(seg.end)} из {_fmt(dur)}", end="", flush=True)
        print()
        lines = split_lines_fix(lines)
        if not args.language:
            print(f"  язык: {info.language} (уверенность {info.language_probability:.0%})")
        srt = path.with_name(f"{path.stem}{SUFFIX}.srt")
        txt = path.with_name(f"{path.stem}{SUFFIX}.txt")
        srt.write_text(to_srt(lines), encoding="utf-8")
        txt.write_text(to_dated(lines, start), encoding="utf-8")
        for p in (srt, txt):
            _copy_times(path, p)
        made = [srt, txt]
        if args.embed and path.suffix.lower() in VIDEO_EXTS:
            out = path.with_name(f"{path.stem}{SUFFIX}.mp4")
            lang = {"ru": "rus", "en": "eng", "de": "deu", "fr": "fra", "es": "spa", "tr": "tur",
                    "uk": "ukr", "it": "ita"}.get(info.language, "und")
            proc = subprocess.run([args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                                   "-i", str(path), "-i", str(srt), "-map", "0", "-map", "1:0", "-c", "copy",
                                   "-c:s", "mov_text", "-metadata:s:s:0", f"language={lang}",
                                   "-map_metadata", "0", "-movflags", "+faststart", str(out)],
                                  stderr=subprocess.PIPE)
            if proc.returncode == 0:
                _copy_times(path, out)
                made.append(out)
            else:
                print("  не удалось добавить дорожку субтитров: "
                      + proc.stderr.decode("utf-8", errors="replace").strip()[-300:], file=sys.stderr)
        print(f"  строк: {len(lines)}; {_fmt(dur)} за {_fmt(time.monotonic() - t0)}")
        for p in made:
            print(f"  → {p.name}")
    print(f"Время работы: {_fmt(time.monotonic() - started)}")
    return 1 if failed else 0


def split_lines_fix(lines: list[Line]) -> list[Line]:
    """Наложения между строками соседних сегментов и минимальная длительность показа."""
    for i, ln in enumerate(lines):
        nxt = lines[i + 1].start if i + 1 < len(lines) else None
        ln.end = max(ln.end, ln.start + 0.8)
        if nxt is not None and ln.end > nxt:
            ln.end = max(ln.start + 0.1, nxt)
    return lines


def add_parser(sub) -> None:
    p = sub.add_parser(
        "transcribe", help="расшифровать речь в видео: субтитры .srt и текст с датой и временем",
        description="Распознаёт речь в роликах (Whisper, локально, без интернета после первой загрузки модели). "
                    "Рядом с роликом: <имя>_transcribe.srt (субтитры) и <имя>_transcribe.txt (строки "
                    "«ГГГГ-ММ-ДД ЧЧ:ММ:СС: текст», время = дата съёмки + смещение в ролике). "
                    "Нужен пакет: pip install faster-whisper")
    p.add_argument("files", nargs="+", metavar="ФАЙЛ", help="ролики: имена или маски, например \"E:\\video\\*.mp4\"")
    p.add_argument("--language", metavar="КОД", help="язык речи: ru, en, ... (по умолчанию определяется сам)")
    p.add_argument("--model", default="large-v3-turbo",
                   help="модель Whisper: large-v3-turbo (по умолчанию: быстрая и точная), large-v3 "
                        "(точнее, в 3–4 раза медленнее), medium, small (быстрее, грубее)")
    p.add_argument("--embed", action="store_true",
                   help="ещё и копия ролика <имя>_transcribe.mp4 с дорожкой субтитров (без перекодировки)")
    p.add_argument("--tz", help="часовой пояс съёмки (Europe/Moscow или +10:00), если определился неверно")
    p.add_argument("--start-time", metavar="'ГГГГ-ММ-ДД ЧЧ:ММ:СС'",
                   help="время начала записи вручную (для всех файлов), вместо даты из метаданных")
    p.add_argument("--beam", type=int, default=5, help="ширина поиска (5; 1 — быстрее, чуть грубее)")
    p.add_argument("--threads", type=int, help="потоков процессора (по умолчанию все)")
    p.add_argument("--no-vad", action="store_true",
                   help="не вырезать паузы и шум перед распознаванием (по умолчанию вырезаются: меньше "
                        "«выдуманного» текста в тишине)")
    p.set_defaults(func=transcribe)
