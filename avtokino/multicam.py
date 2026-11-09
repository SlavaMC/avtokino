"""Команда multicam: общий ролик из двух камер, снимавших одновременно.

Файлы камер A и B раскладываются по времени съёмки (метаданные + поправка часов B),
пересекающиеся пары точно сводятся по звуку (как в команде sync), связанные цепочки файлов
становятся группами, и для каждой группы собирается ролик, где камеры чередуются каждые
N секунд. Звук — от камеры на экране или принудительно от одной камеры.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from .config import STANDARD_FPS, VIDEO_EXTS
from .dating import estimate_offset, resolve
from .probe import VideoInfo

MULTICAM_RE = re.compile(r"_multicam_\d+(_v\d+)?$", re.IGNORECASE)


@dataclass
class Clip:
    cam: str                    # "A" | "B"
    path: Path
    info: VideoInfo
    meta_start: datetime        # местное время по метаданным (для B — уже с поправкой --offset)
    source: str
    start: float | None = None  # точное начало на шкале группы, с
    group: int | None = None
    syncs: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def duration(self) -> float:
        return self.info.duration

    @property
    def has_audio(self) -> bool:
        return self.info.has_audio


@dataclass
class Piece:
    t0: float                   # на шкале группы
    t1: float
    video: Clip
    audio: Clip | None


class Report:
    """Печатает в консоль и копит строки для файла отчёта."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)


def parse_offset(value: str) -> float:
    """«+3:07», «-187», «1:02:03.5», «+3:07.25» -> секунды (на сколько часы B отстают от A)."""
    m = re.fullmatch(r"\s*([+-−]?)\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:[.,]\d+)?)\s*", value)
    if not m:
        raise ValueError(f"ожидается [+|-][ЧЧ:]ММ:СС или секунды, получено: {value}")
    sign = -1 if m[1] in ("-", "−") else 1
    parts = [x for x in (m[2], m[3]) if x is not None]
    sec = float(m[4].replace(",", "."))
    for i, x in enumerate(reversed(parts)):
        sec += int(x) * 60 ** (i + 1)
    return sign * sec


def _files(n: int) -> str:
    word = "файл" if n % 10 == 1 and n % 100 != 11 else \
        "файла" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "файлов"
    return f"{n} {word}"


def _fmt_clock(sec: float) -> str:
    if abs(sec) < 0.005:
        sec = 0.0
    sign = "−" if sec < 0 else ""
    sec = abs(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{sign}{int(h)}:{int(m):02d}:{s:05.2f}" if h else f"{sign}{int(m)}:{s:05.2f}"


def _snap_fps(fps: Fraction) -> Fraction:
    if not fps:
        return Fraction(30)
    best = min(STANDARD_FPS, key=lambda f: abs(float(f) - float(fps)))
    return best if abs(float(best) - float(fps)) < 0.5 else fps


def _components(clips: list[Clip], edges: list[tuple[Clip, Clip, float]]) -> list[list[Clip]]:
    """Связные цепочки по найденным сдвигам; внутри каждой расставляет точное время начала."""
    adj: dict[int, list[tuple[Clip, float]]] = {id(c): [] for c in clips}
    for x, y, lag in edges:          # lag = начало y − начало x
        adj[id(x)].append((y, lag))
        adj[id(y)].append((x, -lag))
    groups = []
    seen: set[int] = set()
    for c in sorted(clips, key=lambda c: c.meta_start):
        if id(c) in seen or not adj[id(c)]:
            continue
        c.start = 0.0
        comp, queue = [c], [c]
        seen.add(id(c))
        while queue:
            x = queue.pop(0)
            for y, lag in adj[id(x)]:
                if id(y) not in seen:
                    y.start = x.start + lag
                    seen.add(id(y))
                    comp.append(y)
                    queue.append(y)
        t0 = min(x.start for x in comp)
        for x in comp:
            x.start -= t0
        groups.append(sorted(comp, key=lambda x: x.start))
    return groups


def _plan(group: list[Clip], switch: float, fps: Fraction, audio_cam: str | None,
          overlap_only: bool) -> list[Piece]:
    """Монтаж группы. Время раскладывается по кадрам; там, где снимают обе камеры, они чередуются
    каждые switch секунд, причём отсчёт начинается заново с момента, когда появилась вторая камера,
    и первой показывается только что появившаяся."""
    frame = 1 / float(fps)

    def snap(t: float) -> int:          # время -> номер кадра
        return int(round(t / frame))

    def at(cam: str, f0: int, f1: int) -> Clip | None:
        """Файл камеры, который покрывает весь отрезок кадров [f0, f1)."""
        for c in group:
            if c.cam == cam and snap(c.start) <= f0 and snap(c.start + c.duration) >= f1:
                return c
        return None

    end = snap(max(c.start + c.duration for c in group))
    bounds = sorted({0, end} | {min(end, max(0, snap(t))) for c in group
                                for t in (c.start, c.start + c.duration)})
    step = max(1, snap(switch))
    # 1) какие камеры доступны на каждом отрезке между краями файлов; 2) куда переключаться
    slots: list[tuple[int, int, str]] = []        # (кадр начала, кадр конца, камера на экране)
    shown = None
    run_start = None                               # начало текущего участка, где снимают обе камеры
    first = None
    for f0, f1 in zip(bounds, bounds[1:]):
        if f1 <= f0:
            continue
        has = {cam: at(cam, f0, f1) is not None for cam in "AB"}
        if has["A"] and has["B"]:
            if run_start is None:                  # вторая камера только что появилась: показываем её
                run_start = f0
                first = "A" if shown is None else ("B" if shown == "A" else "A")
            f = f0
            while f < f1:
                k = (f - run_start) // step
                g = min(f1, run_start + (k + 1) * step)
                cam = first if k % 2 == 0 else ("B" if first == "A" else "A")
                slots.append((f, g, cam))
                shown = cam
                f = g
        else:
            run_start = None
            if overlap_only:
                continue
            cam = "A" if has["A"] else "B" if has["B"] else None
            if cam:
                slots.append((f0, f1, cam))
                shown = cam

    pieces: list[Piece] = []
    for f0, f1, cam in slots:
        video = at(cam, f0, f1)
        other = at("B" if cam == "A" else "A", f0, f1)
        audio = video
        if audio_cam:
            forced = video if video.cam == audio_cam else other
            if forced is not None and forced.has_audio:
                audio = forced
        if not audio.has_audio:
            audio = other if other is not None and other.has_audio else None
        t0, t1 = f0 * frame, f1 * frame
        last = pieces[-1] if pieces else None
        if last and last.video is video and last.audio is audio and abs(last.t1 - t0) < frame / 2:
            last.t1 = t1
        else:
            pieces.append(Piece(t0, t1, video, audio))
    # слишком короткие куски (меньше 0,5 с) у краёв файлов сливаем с предыдущим, если тот может продолжиться
    merged: list[Piece] = []
    for p in pieces:
        prev = merged[-1] if merged else None
        if (prev and p.t1 - p.t0 < 0.5 and abs(prev.t1 - p.t0) < frame / 2
                and snap(prev.video.start + prev.video.duration) >= snap(p.t1)):
            prev.t1 = p.t1
        else:
            merged.append(p)
    return merged


def _render_piece(i: int, p: Piece, w: int, h: int, fps: Fraction, args, tmp: Path) -> tuple[Path, Path]:
    dur = p.t1 - p.t0
    frames = max(1, round(dur * float(fps)))
    dur = frames / float(fps)
    vout, aout = tmp / f"v{i:05d}.ts", tmp / f"a{i:05d}.wav"
    src_t = max(0.0, p.t0 - p.video.start)
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps.numerator}/{fps.denominator},"
          f"scale=out_color_matrix=bt709:out_range=tv,format=yuv420p")
    cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-ss", f"{src_t:.3f}", "-i", str(p.video.path), "-map", "0:v:0", "-an",
           "-vf", vf, "-frames:v", str(frames), "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
           "-threads", str(max(1, (os.cpu_count() or 4) // args.jobs))]
    if args.max_bitrate:
        rate = int(args.max_bitrate * 1_000_000)
        cmd += ["-maxrate", str(rate), "-bufsize", str(rate * 2)]
    cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-f", "mpegts", str(vout)]
    _run(cmd)
    # звук ровно той же длины, с короткими затуханиями на краях, чтобы на стыках не щёлкало
    fade = min(0.015, dur / 4)
    af = (f"aresample=48000,apad,atrim=0:{dur:.6f},asetpts=PTS-STARTPTS,"
          f"afade=t=in:d={fade:.3f},afade=t=out:st={dur - fade:.6f}:d={fade:.3f}")
    if p.audio is not None:
        a_t = p.t0 - p.audio.start
        pre = max(0.0, -a_t)          # файл звука начинается чуть позже куска: добиваем тишиной в начале
        if pre > 0:
            af = f"adelay={int(pre * 1000)}:all=1," + af
        cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
               "-ss", f"{max(0.0, a_t):.3f}", "-i", str(p.audio.path), "-map", "0:a:0", "-vn",
               "-af", af, "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(aout)]
    else:
        cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{dur:.6f}",
               "-c:a", "pcm_s16le", str(aout)]
    _run(cmd)
    return vout, aout


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError("ffmpeg: " + " | ".join(err[-4:]))


def _output_path(first: Clip, n: int) -> Path:
    base = f"{first.path.stem}_multicam_{n}"
    out = first.path.with_name(base + ".mp4")
    v = 2
    while out.exists():
        out = first.path.with_name(f"{base}_v{v}.mp4")
        v += 1
    return out


def multicam(args) -> int:
    try:
        import numpy  # noqa: F401
    except ImportError:
        print("Для команды multicam нужен numpy: pip install numpy", file=sys.stderr)
        return 2
    from .tools import _expand_masks, _load_audio, _video_start, find_lag

    try:
        offset = parse_offset(args.offset) if args.offset else 0.0
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    rep = Report()

    def collect(masks: list[str], cam: str) -> list[Path]:
        files = [p for p in _expand_masks(masks)
                 if p.suffix.lower() in VIDEO_EXTS and not MULTICAM_RE.search(p.stem)]
        if not files:
            print(f"Камера {cam}: видеофайлов не найдено", file=sys.stderr)
        return files

    files_a, files_b = collect([args.mask_a], "A"), collect([args.mask_b], "B")
    if not files_a or not files_b:
        return 2
    both = set(files_a) & set(files_b)
    if both:
        print("Маски A и B захватывают одни и те же файлы: " + ", ".join(p.name for p in both), file=sys.stderr)
        return 2

    # --- даты съёмки ---------------------------------------------------------
    raw = []
    for cam, files in (("A", files_a), ("B", files_b)):
        for p in files:
            try:
                info, meta, name, fs, vote = _video_start(args.ffprobe, p)
            except Exception as e:  # noqa: BLE001
                rep(f"Пропуск {p.name}: не удалось прочитать ({e})")
                continue
            if info.duration <= 0:
                rep(f"Пропуск {p.name}: неизвестная длительность")
                continue
            raw.append((cam, p, info, meta, name, fs, vote))
    est = estimate_offset([(r[3], r[4]) for r in raw], [r[6] for r in raw if r[6] is not None])
    tz = est[0] if est else (datetime.now().astimezone().utcoffset() or timedelta(0))
    clips: list[Clip] = []
    for cam, p, info, meta, name, fs, _ in raw:
        dr = resolve(meta, name, fs, tz)
        when = dr.when + (timedelta(seconds=offset) if cam == "B" else timedelta(0))
        clips.append(Clip(cam, p, info, when, dr.source))

    rep(f"Камера A: {_files(sum(c.cam == 'A' for c in clips))} по маске {args.mask_a}")
    rep(f"Камера B: {_files(sum(c.cam == 'B' for c in clips))} по маске {args.mask_b}"
        + (f", поправка часов B {offset:+.2f} с" if offset else ""))
    rep(f"Поиск точного сдвига: ±{args.search:g} с от времени по метаданным")
    rep()
    rep("--- Файлы по времени съёмки (метаданные" + (", B с поправкой" if offset else "") + ") ---")
    for c in sorted(clips, key=lambda c: c.meta_start):
        rep(f"{c.cam}  {c.meta_start:%Y-%m-%d %H:%M:%S}  {_fmt_clock(c.duration):>10}  {c.name}  [{c.source}]")

    # --- пары, пересекающиеся по времени, и точная сверка по звуку -------------
    def span(c: Clip) -> tuple[datetime, datetime]:
        return c.meta_start, c.meta_start + timedelta(seconds=c.duration)

    tol = timedelta(seconds=args.search)
    pairs = []
    for x in (c for c in clips if c.cam == "A"):
        for y in (c for c in clips if c.cam == "B"):
            (x0, x1), (y0, y1) = span(x), span(y)
            if x0 < y1 + tol and y0 < x1 + tol:
                pairs.append((x, y))
    rep()
    rep(f"--- Сверка по звуку, пар файлов, пересекающихся по времени: {len(pairs)} ---")
    audio_cache: dict[Path, object] = {}

    def audio_of(c: Clip):
        if c.path not in audio_cache:
            audio_cache[c.path] = _load_audio(args.ffmpeg, c.path, None)
        return audio_cache[c.path]

    edges = []
    wide_diffs: list[float] = []
    for x, y in pairs:
        expected = (y.meta_start - x.meta_start).total_seconds()  # начало B − начало A
        label = f"{x.name} ↔ {y.name}"
        if not (x.has_audio and y.has_audio):
            rep(f"{label}: у одного из файлов нет звука, пара пропущена")
            continue
        try:
            r = find_lag(audio_of(x), audio_of(y), expected - args.search, expected + args.search)
        except Exception as e:  # noqa: BLE001
            rep(f"{label}: не удалось сверить ({e})")
            continue
        if r is None or not r.reliable:
            # часы могут расходиться сильнее, чем ожидалось: ищем сдвиг без ограничений
            try:
                wide = find_lag(audio_of(x), audio_of(y))
            except Exception:  # noqa: BLE001
                wide = None
            if wide is not None and wide.reliable:
                rep(f"{label}: в пределах ±{args.search:g} с от метаданных общего звука нет, "
                    f"но без ограничения найден надёжный сдвиг (часы B расходятся с A на "
                    f"{wide.lag - expected:+.1f} с)")
                r = wide
                wide_diffs.append(wide.lag - expected)
        if r is None:
            rep(f"{label}: общего звука не нашлось")
            continue
        verdict = "надёжно" if r.reliable else "сомнительно" if r.doubtful else "ненадёжно"
        rel = f"B начат на {r.lag:+.3f} с относительно A (по метаданным {expected:+.1f} с, часы B "
        rel += f"{'отстают' if r.lag - expected > 0 else 'спешат'} ещё на {abs(r.lag - expected):.2f} с)"
        rep(f"{label}: {rel}; {verdict} ({r.text})")
        if r.reliable or (r.doubtful and args.accept_doubtful):
            edges.append((x, y, r.lag))
            x.syncs.append((y, r))
            y.syncs.append((x, r))
        elif r.doubtful:
            rep("    сомнительный сдвиг не используется (принять: --accept-doubtful)")
    audio_cache.clear()
    if wide_diffs and not getattr(args, "_auto_offset", False):
        # Часы сильно расходятся: заново раскладываем файлы с найденной поправкой, иначе часть пар не найдётся.
        import statistics
        fix = offset + statistics.median(wide_diffs)
        rep()
        rep(f"Часы B расходятся с A примерно на {fix:+.1f} с. Повторяю подбор пар с --offset={fix:+.1f}")
        rep("(в следующий раз эту поправку можно указать сразу)")
        rep()
        args.offset = f"{fix:+.2f}"
        args._auto_offset = True
        return multicam(args)

    groups = _components(clips, edges)
    lonely = [c for c in clips if not any(c in g for g in groups)]
    if not groups:
        rep()
        rep("Одновременно снятых файлов с общим звуком не найдено, ролики не собирались.")
        _save_report(rep, None, args)
        return 3

    rc = 0
    for gi, group in enumerate(groups, 1):
        fps = Fraction(args.fps).limit_denominator(1001) if args.fps else \
            _snap_fps(max((c.info.fps for c in group), key=float))
        big = max(group, key=lambda c: c.info.width * c.info.height)
        w, h = args.resolution or (big.info.width, big.info.height)
        w, h = w // 2 * 2, h // 2 * 2
        pieces = _plan(group, args.switch, fps, args.audio, args.overlap_only)
        first = min(group, key=lambda c: c.start)
        out = _output_path(first, len(group))
        total = sum(p.t1 - p.t0 for p in pieces)
        t0_local = first.meta_start - timedelta(seconds=first.start)
        rep()
        rep(f"=== Группа {gi}: {_files(len(group))} → {out.name} ===")
        rep(f"Начало: {t0_local:%Y-%m-%d %H:%M:%S} (по часам {'A' if first.cam == 'A' else 'B с поправкой'}), "
            f"длительность {_fmt_clock(total)}, {w}x{h}, {float(fps):.3f} кадр/с, "
            f"смена камеры каждые {args.switch:g} с, звук: "
            + (f"камера {args.audio} (если у неё есть звук в этот момент)" if args.audio else "от камеры на экране"))
        rep("Файлы группы (время от начала ролика):")
        for c in group:
            rep(f"  {c.cam}  {_fmt_clock(c.start):>10} – {_fmt_clock(c.start + c.duration):>10}  {c.name}")
        rep("Монтаж (время ролика: камера, файл, отрезок файла):")
        for p in pieces:
            vt = p.t0 - p.video.start
            line = (f"  {_fmt_clock(p.t0):>10} – {_fmt_clock(p.t1):>10}  {p.video.cam}  {p.video.name} "
                    f"[{_fmt_clock(vt)} – {_fmt_clock(vt + p.t1 - p.t0)}]")
            if p.audio is None:
                line += "  звук: тишина"
            elif p.audio is not p.video:
                line += f"  звук: {p.audio.cam} {p.audio.name}"
            rep(line)
        if args.dry_run:
            continue
        try:
            _render_group(pieces, w, h, fps, out, t0_local, tz, args, rep)
        except Exception as e:  # noqa: BLE001
            rep(f"ОШИБКА сборки группы {gi}: {e}")
            rc = 1
            continue
        rep(f"Готово: {out}")
    if lonely:
        rep()
        rep("Не вошли ни в одну группу (нет одновременной записи с общим звуком):")
        for c in sorted(lonely, key=lambda c: c.meta_start):
            rep(f"  {c.cam}  {c.meta_start:%Y-%m-%d %H:%M:%S}  {c.name}")
    if args.dry_run:
        rep()
        rep("Режим --dry-run: ролики не собирались")
    _save_report(rep, groups, args)
    return rc


def _render_group(pieces, w, h, fps, out: Path, t0_local: datetime, tz: timedelta, args, rep) -> None:
    with tempfile.TemporaryDirectory(prefix="multicam_", dir=args.temp_dir) as td:
        tmp = Path(td)
        done = 0
        results: list[tuple[Path, Path]] = [None] * len(pieces)  # type: ignore[list-item]
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_render_piece, i, p, w, h, fps, args, tmp): i for i, p in enumerate(pieces)}
            for f in futures:
                results[futures[f]] = f.result()
                done += 1
                print(f"\r  кусков: {done}/{len(pieces)}", end="", flush=True)
        print()
        vlist, alist = tmp / "v.txt", tmp / "a.txt"
        vlist.write_text("".join(f"file '{v.name}'\n" for v, _ in results), encoding="utf-8")
        alist.write_text("".join(f"file '{a.name}'\n" for _, a in results), encoding="utf-8")
        created = (t0_local - tz).replace(tzinfo=timezone.utc)
        _run([args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
              "-f", "concat", "-safe", "0", "-i", str(vlist),
              "-f", "concat", "-safe", "0", "-i", str(alist),
              "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-metadata", "creation_time=" + created.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
              "-movflags", "+faststart", str(out)])
    ts = created.timestamp()
    os.utime(out, (ts, ts))


def _save_report(rep: Report, groups, args) -> None:
    if args.dry_run or not groups:
        return
    first = min(groups[0], key=lambda c: c.start)
    path = first.path.with_name(f"multicam_{datetime.now():%Y%m%d_%H%M%S}.log")
    try:
        path.write_text("\n".join(rep.lines) + "\n", encoding="utf-8")
        print(f"Отчёт: {path}")
    except OSError:
        pass


def add_parser(sub, parse_resolution) -> None:
    m = sub.add_parser(
        "multicam", help="общий ролик из двух камер, снимавших одновременно",
        description="Ищет одновременно снятые файлы камер A и B, точно сводит их по звуку и для каждой "
                    "группы пересекающихся файлов собирает ролик, где камеры чередуются. Ролик называется "
                    "по самому раннему файлу группы: <имя>_multicam_<число файлов>.mp4.")
    m.add_argument("mask_a", metavar="МАСКА_A", help="файлы камеры A, например \"E:\\\\video\\\\GOPR*.MP4\"")
    m.add_argument("mask_b", metavar="МАСКА_B", help="файлы камеры B, например \"E:\\\\video\\\\SJCM*.mp4\"")
    m.add_argument("--offset", metavar="±[ЧЧ:]ММ:СС",
                   help="примерно на сколько часы B отстают от A, например +3:07; если спешат: --offset=-3:07")
    m.add_argument("--search", type=float, default=60.0, metavar="СЕК",
                   help="точный сдвиг ищется в пределах ±СЕК от времени по метаданным (60)")
    m.add_argument("--switch", type=float, default=5.0, metavar="СЕК", help="смена камеры каждые СЕК секунд (5)")
    m.add_argument("--audio", choices=["A", "B"],
                   help="звук всегда от этой камеры, когда у неё есть звук (по умолчанию от камеры на экране)")
    m.add_argument("--overlap-only", action="store_true",
                   help="только время, когда снимали обе камеры (по умолчанию и участки с одной камерой)")
    m.add_argument("--accept-doubtful", action="store_true", help="использовать и сомнительные сдвиги")
    m.add_argument("--resolution", type=parse_resolution, metavar="ШxВ",
                   help="разрешение (по умолчанию наибольшее в группе)")
    m.add_argument("--fps", type=float, help="частота кадров (по умолчанию как у камеры с большим fps)")
    m.add_argument("--crf", type=int, default=20, help="качество x264, меньше = лучше (20)")
    m.add_argument("--preset", default="medium", help="пресет x264 (medium); быстрее: veryfast")
    m.add_argument("--max-bitrate", type=float, metavar="МБИТ", help="потолок видеопотока, Мбит/с")
    m.add_argument("--jobs", type=int, default=2, help="кусков рендерить параллельно (2)")
    m.add_argument("--temp-dir", type=Path, help="каталог для временных файлов")
    m.add_argument("--dry-run", action="store_true", help="только отчёт и план, без сборки")
    m.set_defaults(func=multicam)
