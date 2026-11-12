"""Команда pano360: ролик из панорам 360 (фото и видео в равнопромежуточной развёртке 2:1).

Режимы:
  flat — обычный кадр 16:9: камера медленно поворачивается по кругу (фото и видео);
  360  — полноценный ролик 360 с пометкой сферической проекции (YouTube, VR-плееры).

Поворот сделан без пересчёта карты v360 на каждом кадре: панорама ставится рядом сама с собой
(hstack), из неё вырезается окно шириной в одну панораму со сдвигом по времени — это и есть
поворот по горизонтали, — а v360 с постоянными параметрами превращает окно в обычный кадр.
"""
from __future__ import annotations

import fnmatch
import math
import os
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from .config import PHOTO_EXTS, VIDEO_EXTS


@dataclass
class PanoItem:
    path: Path
    kind: str                 # photo | video
    width: int
    height: int
    duration: float           # для фото — сколько показывать
    when: datetime
    source: str
    has_audio: bool = False
    fps: Fraction | None = None


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError("ffmpeg: " + " | ".join(err[-4:]))


def _collect(folder: Path, mask: str, args) -> tuple[list[PanoItem], list[str]]:
    from .dating import check_utc, estimate_offset, fs_date, name_date, photo_meta, resolve, video_meta
    from .probe import probe_photo, probe_video

    skipped: list[str] = []
    raw = []
    out_prefix = folder.name.casefold() + "_pano"
    for p in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if not p.is_file() or not fnmatch.fnmatch(p.name.casefold(), mask.casefold()):
            continue
        ext = p.suffix.lower()
        if p.stem.casefold().startswith(out_prefix):
            continue                      # ранее собранные ролики
        try:
            if ext in PHOTO_EXTS:
                info = probe_photo(p)
                raw.append(("photo", p, info, photo_meta(info), name_date(p), fs_date(p), None))
            elif ext in VIDEO_EXTS:
                info = probe_video(args.ffprobe, p)
                fs = fs_date(p)
                meta, vote = check_utc(video_meta(info), fs, info.duration)
                raw.append(("video", p, info, meta, name_date(p), fs, vote))
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{p.name}: не удалось прочитать ({e})")
    est = estimate_offset([(r[3], r[4]) for r in raw], [r[6] for r in raw if r[6] is not None])
    tz = est[0] if est else (datetime.now().astimezone().utcoffset() or timedelta(0))
    items = []
    for kind, p, info, meta, name, fs, _ in raw:
        if abs(info.width - 2 * info.height) > info.height * 0.02:
            skipped.append(f"{p.name}: {info.width}x{info.height} — не панорама 2:1, пропущен")
            continue
        dr = resolve(meta, name, fs, tz)
        if kind == "photo":
            items.append(PanoItem(p, kind, info.width, info.height, args.photo_duration, dr.when, dr.source))
        else:
            items.append(PanoItem(p, kind, info.width, info.height, info.duration, dr.when, dr.source,
                                  info.has_audio, info.fps))
    # по времени съёмки, при равенстве — по имени
    items.sort(key=lambda it: (it.when, it.path.name.casefold()))
    return items, skipped


def _rotation_chain(args, w: int, h: int, offset: float) -> str:
    """Поворот камеры: окно, скользящее по удвоенной панораме, и v360 в обычный кадр."""
    sign = 1 if args.direction == "right" else -1
    v_fov = math.degrees(2 * math.atan(math.tan(math.radians(args.fov) / 2) * h / w))
    # x — сдвиг окна; offset продолжает поворот с того места, где закончился предыдущий файл
    x = f"mod(({offset:.6f}+t)/{args.rotation:.6f}*in_w/2*{sign}+in_w*2\\,in_w/2)"
    return (f"split[a][b];[a][b]hstack,crop=w=in_w/2:h=in_h:x='{x}',"
            f"v360=e:flat:yaw={args.yaw:g}:pitch={args.pitch:g}:h_fov={args.fov:g}:v_fov={v_fov:.4f}"
            f":w={w}:h={h}:interp=cubic")


def _render(i: int, it: PanoItem, args, w: int, h: int, fps: Fraction, offset: float, tmp: Path):
    vout, aout = tmp / f"v{i:04d}.ts", tmp / f"a{i:04d}.wav"
    frames = max(1, round(it.duration * float(fps)))
    dur = frames / float(fps)
    rate = f"{fps.numerator}/{fps.denominator}"
    enc = ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf), "-pix_fmt", "yuv420p",
           "-threads", str(max(1, (os.cpu_count() or 4) // args.jobs))]
    if args.max_bitrate:
        r = int(args.max_bitrate * 1_000_000)
        enc += ["-maxrate", str(r), "-bufsize", str(2 * r)]
    if it.kind == "photo":
        # фото декодируется один раз и размножается фильтром loop (с -loop 1 огромный JPEG
        # декодировался бы на каждом кадре)
        pre = f"format=yuv420p,loop=loop={frames - 1}:size=1:start=0,setpts=N/({rate})/TB"
        inp = ["-i", str(it.path)]
    else:
        pre = f"fps={rate},format=yuv420p"
        inp = ["-i", str(it.path)]
    if args.mode == "flat":
        chain = f"[0:v]{pre}," + _rotation_chain(args, w, h, offset) + ",format=yuv420p[v]"
    else:
        chain = f"[0:v]{pre},scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p[v]"
    _run([args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"] + inp +
         ["-filter_complex", chain, "-map", "[v]", "-frames:v", str(frames), "-r", rate] + enc +
         ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-f", "mpegts", str(vout)])
    if it.kind == "video" and it.has_audio:
        af = f"aresample=48000,apad,atrim=0:{dur:.6f},asetpts=PTS-STARTPTS"
        _run([args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(it.path),
              "-map", "0:a:0", "-vn", "-af", af, "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(aout)])
    else:
        _run([args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
              "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{dur:.6f}", "-c:a", "pcm_s16le", str(aout)])
    return vout, aout, dur


# --- пометка сферической проекции (Spherical Video V2: st3d + sv3d) ---------------

def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _full_box(kind: bytes, payload: bytes) -> bytes:
    return _box(kind, b"\0\0\0\0" + payload)


def spherical_boxes() -> bytes:
    st3d = _full_box(b"st3d", b"\0")                                   # моно
    svhd = _full_box(b"svhd", b"avtokino\0")
    prhd = _full_box(b"prhd", struct.pack(">iii", 0, 0, 0))             # yaw, pitch, roll
    equi = _full_box(b"equi", struct.pack(">IIII", 0, 0, 0, 0))         # без обрезки
    sv3d = _box(b"sv3d", svhd + _box(b"proj", prhd + equi))
    return st3d + sv3d


def inject_spherical(path: Path) -> bool:
    """Добавляет st3d/sv3d в описание видеодорожки MP4. moov должен быть в конце файла
    (без +faststart), тогда вставка не сдвигает данные и таблицы смещений не меняются."""
    data = bytearray(path.read_bytes())

    def children(start: int, end: int):
        pos = start
        while pos + 8 <= end:
            size, kind = struct.unpack(">I4s", data[pos:pos + 8])
            hdr = 8
            if size == 1:
                size, hdr = struct.unpack(">Q", data[pos + 8:pos + 16])[0], 16
            elif size == 0:
                size = end - pos
            yield pos, size, kind, hdr
            pos += size

    top = {k: (p, s, hd) for p, s, k, hd in children(0, len(data))}
    if b"moov" not in top or b"mdat" not in top or top[b"moov"][0] < top[b"mdat"][0]:
        return False
    chain = []                                 # (позиция, размер заголовка) всех предков для правки размеров
    m_pos, m_size, m_hdr = top[b"moov"]
    for tpos, tsize, tkind, thdr in children(m_pos + m_hdr, m_pos + m_size):
        if tkind != b"trak":
            continue
        path_ = [(tpos, thdr)]
        node = (tpos, tsize, thdr)
        ok = True
        for want in (b"mdia", b"minf", b"stbl", b"stsd"):
            found = next(((p, s, hd) for p, s, k, hd in children(node[0] + node[2], node[0] + node[1])
                          if k == want), None)
            if not found:
                ok = False
                break
            node = found
            path_.append((found[0], found[2]))
        if not ok:
            continue
        stsd_pos, stsd_size, stsd_hdr = node
        entry_pos = stsd_pos + stsd_hdr + 8      # версия/флаги + число записей
        size, kind = struct.unpack(">I4s", data[entry_pos:entry_pos + 8])
        if kind not in (b"avc1", b"hvc1", b"hev1"):
            continue
        chain = [(m_pos, m_hdr)] + path_ + [(entry_pos, 8)]
        insert_at = entry_pos + size
        break
    if not chain:
        return False
    boxes = spherical_boxes()
    for pos, hdr in chain:
        if hdr == 16:
            cur = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            data[pos + 8:pos + 16] = struct.pack(">Q", cur + len(boxes))
        else:
            cur = struct.unpack(">I", data[pos:pos + 4])[0]
            data[pos:pos + 4] = struct.pack(">I", cur + len(boxes))
    data[insert_at:insert_at] = boxes
    path.write_bytes(bytes(data))
    return True


def _output(folder: Path, mode: str) -> Path:
    base = f"{folder.name}_pano{'360' if mode == '360' else ''}"
    out = folder / f"{base}.mp4"
    n = 2
    while out.exists():
        out = folder / f"{base}_v{n}.mp4"
        n += 1
    return out


def _fmt(sec: float) -> str:
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:05.2f}"


def pano360(args) -> int:
    folder = Path(args.dir).resolve()
    if not folder.is_dir():
        print(f"Каталог не найден: {folder}", file=sys.stderr)
        return 2
    items, skipped = _collect(folder, args.mask, args)
    for s_ in skipped:
        print(s_)
    if not items:
        print("Панорам 360 по маске не найдено", file=sys.stderr)
        return 2

    videos = [it for it in items if it.kind == "video"]
    if args.fps:
        fps = Fraction(args.fps).limit_denominator(1001)
    elif videos:
        fps = max((it.fps for it in videos if it.fps), key=float, default=Fraction(30))
        fps = Fraction(30000, 1001) if abs(float(fps) - 29.97) < 0.05 else fps.limit_denominator(1001)
    else:
        fps = Fraction(30)
    if args.mode == "flat":
        w, h = args.resolution or (1920, 1080)
    else:
        if args.resolution:
            w, h = args.resolution
        else:
            src = max(videos or items, key=lambda it: it.width)
            w = min(src.width, 7680)
            h = w // 2
    w, h = w // 2 * 2, h // 2 * 2
    out = _output(folder, args.mode)

    print(f"Файлов: {len(items)} (фото {len(items) - len(videos)}, видео {len(videos)})")
    if args.mode == "flat":
        print(f"Режим: обычный кадр {w}x{h}, {float(fps):.3f} кадр/с; угол обзора {args.fov:g}°, наклон "
              f"{args.pitch:g}°, поворот {'вправо' if args.direction == 'right' else 'влево'}, "
              f"1 оборот за {args.rotation:g} с; фото по {args.photo_duration:g} с")
    else:
        print(f"Режим: ролик 360, {w}x{h}, {float(fps):.3f} кадр/с; фото по {args.photo_duration:g} с")
    print(f"Ролик: {out}")
    print()
    t = 0.0
    for it in items:
        print(f"  {_fmt(t):>8}  {it.when:%Y-%m-%d %H:%M:%S}  {'фото ' if it.kind == 'photo' else 'видео'}  "
              f"{_fmt(it.duration):>8}  {it.path.name}  [{it.source}]")
        t += it.duration
    print(f"Длительность: {_fmt(t)}")
    if args.dry_run:
        print("Режим --dry-run: ролик не собирался")
        return 0

    # поворот продолжается от файла к файлу без скачков
    offsets, acc = [], 0.0
    for it in items:
        offsets.append(acc if args.continuous else 0.0)
        acc += it.duration
    with tempfile.TemporaryDirectory(prefix="pano_", dir=args.temp_dir) as td:
        tmp = Path(td)
        results = [None] * len(items)
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futs = {pool.submit(_render, i, it, args, w, h, fps, offsets[i], tmp): i for i, it in enumerate(items)}
            done = 0
            for f, i in futs.items():
                try:
                    results[i] = f.result()
                except Exception as e:  # noqa: BLE001
                    print(f"\nОШИБКА {items[i].path.name}: {e}", file=sys.stderr)
                    return 1
                done += 1
                print(f"\r  готово файлов: {done}/{len(items)}", end="", flush=True)
        print()
        (tmp / "v.txt").write_text("".join(f"file '{r[0].name}'\n" for r in results), encoding="utf-8")
        (tmp / "a.txt").write_text("".join(f"file '{r[1].name}'\n" for r in results), encoding="utf-8")
        first = items[0]
        created = first.when.astimezone(timezone.utc) if first.when.tzinfo else \
            first.when.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(timezone.utc)
        tag = ["-metadata", "creation_time=" + created.strftime("%Y-%m-%dT%H:%M:%S.000000Z")]
        if args.mode == "flat":
            _run([args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                  "-f", "concat", "-safe", "0", "-i", str(tmp / "v.txt"),
                  "-f", "concat", "-safe", "0", "-i", str(tmp / "a.txt"),
                  "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"] + tag +
                 ["-movflags", "+faststart", str(out)])
        else:
            raw = tmp / "raw.mp4"
            _run([args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                  "-f", "concat", "-safe", "0", "-i", str(tmp / "v.txt"),
                  "-f", "concat", "-safe", "0", "-i", str(tmp / "a.txt"),
                  "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"] + tag + [str(raw)])
            if not inject_spherical(raw):
                print("Не удалось записать пометку 360 в файл", file=sys.stderr)
                return 1
            # перенос moov в начало; ffmpeg сохраняет пометку 360 только с -strict unofficial
            _run([args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(raw),
                  "-map", "0", "-c", "copy", "-strict", "unofficial", "-movflags", "+faststart", str(out)])
    ts = created.timestamp()
    os.utime(out, (ts, ts))
    print(f"Готово: {out}")
    return 0


def add_parser(sub, parse_resolution, seconds) -> None:
    ep = "Время (СЕК) можно задавать в секундах (90, 2.5) или как ЧЧ:ММ:СС / ММ:СС (01:30:00, 1:30)."
    p = sub.add_parser(
        "pano360", epilog=ep, help="ролик из панорам 360: облёт в обычном кадре или полноценный 360",
        description="Собирает ролик из панорам 360 (фото и видео в развёртке 2:1) каталога. Порядок — по "
                    "времени съёмки, затем по имени. Режим flat: обычный кадр 16:9, камера поворачивается по кругу. "
                    "Режим 360: ролик 360 с пометкой сферической проекции для YouTube и VR-плееров. "
                    "Результат: <каталог>_pano.mp4 или <каталог>_pano360.mp4 в том же каталоге.")
    p.add_argument("dir", help="каталог с панорамами")
    p.add_argument("mask", nargs="?", default="*", help="маска файлов, например \"360_*\" (по умолчанию все)")
    p.add_argument("--mode", choices=["flat", "360"], default="flat",
                   help="flat — обычный кадр с поворотом камеры (по умолчанию), 360 — ролик 360")
    p.add_argument("--photo-duration", type=seconds, default=15.0, metavar="СЕК", help="сколько показывать фото (15)")
    p.add_argument("--rotation", type=seconds, default=8.0, metavar="СЕК",
                   help="flat: один полный оборот камеры за СЕК секунд (8)")
    p.add_argument("--direction", choices=["right", "left"], default="right", help="flat: куда поворачивать (right)")
    p.add_argument("--fov", type=float, default=90.0, metavar="ГРАД", help="flat: угол обзора по горизонтали (90)")
    p.add_argument("--pitch", type=float, default=0.0, metavar="ГРАД",
                   help="flat: наклон камеры, плюс — вверх, минус — вниз (0)")
    p.add_argument("--yaw", type=float, default=0.0, metavar="ГРАД",
                   help="flat: с какого направления начинать поворот (0 — центр панорамы)")
    p.add_argument("--no-continuous", dest="continuous", action="store_false",
                   help="flat: каждый файл начинать с исходного направления (по умолчанию поворот продолжается)")
    p.add_argument("--resolution", type=parse_resolution, metavar="ШxВ",
                   help="разрешение: flat — 1920x1080 по умолчанию; 360 — как у самого большого видео (2:1)")
    p.add_argument("--fps", type=float, help="частота кадров (по умолчанию как у видео, иначе 30)")
    p.add_argument("--crf", type=int, default=20, help="качество x264, меньше = лучше (20)")
    p.add_argument("--preset", default="medium", help="пресет x264 (medium); быстрее: veryfast")
    p.add_argument("--max-bitrate", type=float, metavar="МБИТ", help="потолок видеопотока, Мбит/с")
    p.add_argument("--jobs", type=int, default=2, help="файлов обрабатывать параллельно (2)")
    p.add_argument("--temp-dir", type=Path, help="каталог для временных файлов")
    p.add_argument("--dry-run", action="store_true", help="только список и порядок файлов")
    p.set_defaults(func=pano360)
