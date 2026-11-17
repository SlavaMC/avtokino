"""Команда stitch360: сшивка видео 360 из двух полусфер («рыбий глаз»), как пишет Samsung Gear 360.

Кадр исходника — два круга рядом: передний объектив слева, задний справа. Сшивка:
  1. калибровка по нескольким кадрам — угол обзора объективов, центры кругов и взаимный поворот
     подбираются так, чтобы картинки совпали в зоне перекрытия (одна на все файлы: камера одна);
  2. яркость и цвет двух объективов подравниваются по зоне перекрытия (для каждого файла);
  3. рендер штатными фильтрами ffmpeg: две карты remap в равнопромежуточную развёртку 2:1
     и смешивание с плавным переходом на шве (maskedmerge);
  4. пометка 360 (sv3d и XMP), дата съёмки в метаданных и даты файла — как у исходника.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SUFFIX = "_stitch"
SERIES_SUFFIX = "_stitch_series"
BLEND_DEG = 6.0          # ширина плавного перехода на шве, градусы
UPSCALE = 2              # remap берёт ближайший пиксель; вход увеличивается вдвое — шов и детали без «лесенки»


# --- геометрия -------------------------------------------------------------------

def _rot(yaw: float, pitch: float, roll: float) -> np.ndarray:
    y, p, r = np.radians([yaw, pitch, roll])
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
    rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
    rz = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    return ry @ rx @ rz


def _dirs(w: int, h: int, rows: slice | None = None) -> np.ndarray:
    lon = (np.arange(w) + 0.5) / w * 2 * np.pi - np.pi
    lat = np.pi / 2 - (np.arange(h) + 0.5) / h * np.pi
    if rows is not None:
        lat = lat[rows]
    lon, lat = np.meshgrid(lon, lat)
    return np.stack([np.cos(lat) * np.sin(lon), np.sin(lat), np.cos(lat) * np.cos(lon)], -1)


@dataclass
class Calib:
    fov: float = 195.0                 # угол обзора объектива (по диаметру круга), градусы
    cx_front: float = 0.0              # центры кругов в своих половинах кадра, пиксели
    cy_front: float = 0.0
    cx_back: float = 0.0
    cy_back: float = 0.0
    yaw: float = 0.0                   # поворот заднего объектива относительно переднего, градусы
    pitch: float = 0.0
    roll: float = 0.0
    error: float | None = None         # несовпадение на шве после калибровки (меньше — лучше)

    def vector(self) -> np.ndarray:
        return np.array([self.fov, self.cx_front, self.cy_front, self.cx_back, self.cy_back,
                         self.yaw, self.pitch, self.roll], float)

    @classmethod
    def from_vector(cls, v, error=None) -> "Calib":
        return cls(*[float(x) for x in v], error=error)


def _lens(d: np.ndarray, c: Calib, back: bool, size: int):
    """Направления -> координаты в круге объектива (равноугольный «рыбий глаз»)."""
    if back:
        r = _rot(c.yaw + 180, c.pitch, c.roll)
        cx, cy = c.cx_back, c.cy_back
    else:
        r = np.eye(3)
        cx, cy = c.cx_front, c.cy_front
    dl = d @ r
    theta = np.arccos(np.clip(dl[..., 2], -1, 1))
    phi = np.arctan2(dl[..., 1], dl[..., 0])
    rad = theta / math.radians(c.fov / 2) * (size / 2)
    return cx + rad * np.cos(phi), cy - rad * np.sin(phi), theta


def _sample(img: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    u = np.clip(u, 0, w - 1.001)
    v = np.clip(v, 0, h - 1.001)
    x0, y0 = u.astype(np.int32), v.astype(np.int32)
    fx, fy = u - x0, v - y0
    if img.ndim == 3:
        fx, fy = fx[..., None], fy[..., None]
    a, b = img[y0, x0], img[y0, x0 + 1]
    c, d = img[y0 + 1, x0], img[y0 + 1, x0 + 1]
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


# --- исходники ---------------------------------------------------------------------

@dataclass
class Source:
    path: Path
    width: int
    height: int
    duration: float
    fps: str
    has_audio: bool
    creation_time: str | None
    video_duration: float = 0.0


def _probe(ffprobe: str, p: Path) -> Source | None:
    from .probe import ffprobe_json
    data = ffprobe_json(ffprobe, p)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        return None
    fmt = data.get("format", {})
    tags = {k.lower(): x for k, x in (fmt.get("tags") or {}).items()}
    return Source(p, int(v["width"]), int(v["height"]), float(fmt.get("duration") or v.get("duration") or 0),
                  v.get("avg_frame_rate") or v.get("r_frame_rate") or "30",
                  any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
                  tags.get("creation_time"), float(v.get("duration") or fmt.get("duration") or 0))


def _spherical(ffprobe: str, p: Path) -> bool:
    from .probe import probe_video
    try:
        info = probe_video(ffprobe, p)
    except Exception:  # noqa: BLE001
        return False
    return bool(info and info.spherical)


def _frame(ffmpeg: str, src: Source, t: float) -> np.ndarray:
    out = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{t:.3f}", "-i", str(src.path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    need = src.width * src.height * 3
    if len(out.stdout) < need:
        raise RuntimeError(f"не удалось прочитать кадр {t:.1f} с")
    return np.frombuffer(out.stdout[:need], np.uint8).reshape(src.height, src.width, 3)


def _looks_dual_fisheye(img: np.ndarray) -> bool:
    """Два круга: углы обеих половин кадра почти чёрные, а центры кругов — нет."""
    h, w = img.shape[:2]
    k = max(4, h // 40)
    g = img.mean(2)
    corners = [g[:k, :k], g[:k, w // 2 - k:w // 2], g[:k, w // 2:w // 2 + k], g[:k, -k:],
               g[-k:, :k], g[-k:, w // 2 - k:w // 2], g[-k:, w // 2:w // 2 + k], g[-k:, -k:]]
    centers = [g[h // 2 - k:h // 2 + k, w // 4 - k:w // 4 + k], g[h // 2 - k:h // 2 + k, 3 * w // 4 - k:3 * w // 4 + k]]
    return max(float(c.mean()) for c in corners) < 30 and min(float(c.mean()) for c in centers) > 20


# --- калибровка -----------------------------------------------------------------------

def _highpass(img: np.ndarray) -> np.ndarray:
    from PIL import Image, ImageFilter
    im = Image.fromarray(img).convert("L")
    return np.asarray(im, np.float32) - np.asarray(im.filter(ImageFilter.GaussianBlur(6)), np.float32)


def calibrate(frames: list[np.ndarray], log=print) -> Calib:
    """Подбор параметров объективов по зоне перекрытия (покоординатный спуск)."""
    h, w = frames[0].shape[:2]
    half = w // 2
    hps = [_highpass(f) for f in frames]
    d = _dirs(1536, 768)
    lat = np.arcsin(d[..., 1])
    # полоса вдоль шва, без полюсов (внизу обычно палка/рука)
    band = (np.abs(d[..., 2]) < math.sin(math.radians(6))) & (lat > math.radians(-55)) & (lat < math.radians(70))
    D = d[band]

    def cost(v) -> float:
        c = Calib.from_vector(v)
        uf, vf, _ = _lens(D, c, False, h)
        ub, vb, _ = _lens(D, c, True, h)
        e = 0.0
        for hp in hps:
            e += float(np.mean(np.abs(_sample(hp[:, :half], uf, vf) - _sample(hp[:, half:], ub, vb))))
        return e / len(hps)

    p = Calib(195.0, half / 2, h / 2, half / 2, h / 2).vector()
    steps = np.array([2, 4, 4, 4, 4, 0.5, 0.5, 0.5])
    steps[1:5] *= h / 1920
    best = cost(p)
    start = best
    for _ in range(60):
        improved = False
        for i in range(len(p)):
            for sgn in (1, -1):
                while True:
                    q = p.copy()
                    q[i] += sgn * steps[i]
                    c = cost(q)
                    if c < best - 1e-4:
                        p, best, improved = q, c, True
                    else:
                        break
        if not improved:
            steps /= 2
            if steps[0] < 0.02:
                break
    log(f"  несовпадение на шве: {start:.2f} → {best:.2f}")
    return Calib.from_vector(p, best)


# --- карты и рендер ----------------------------------------------------------------------

def _pgm16(path: Path, a: np.ndarray) -> None:
    a = np.clip(np.round(a), 0, 65535).astype(">u2")
    with open(path, "wb") as f:
        f.write(f"P5\n{a.shape[1]} {a.shape[0]}\n65535\n".encode())
        f.write(a.tobytes())


def build_maps(c: Calib, w: int, h: int, tmp: Path) -> dict:
    """Карты remap (вход увеличен в UPSCALE раз) и маска шва для выхода w x h."""
    half = w // 2
    out = {}
    m = np.empty((h, w), np.uint8)
    xs = {k: np.empty((h, w), np.float32) for k in ("xf", "yf", "xb", "yb")}
    for r0 in range(0, h, 240):          # по полосам — меньше памяти
        rows = slice(r0, min(h, r0 + 240))
        d = _dirs(w, h, rows)
        uf, vf, tf = _lens(d, c, False, h)
        ub, vb, tb = _lens(d, c, True, h)
        xs["xf"][rows] = (np.clip(uf, 0, half - 1) + 0.5) * UPSCALE - 0.5
        xs["yf"][rows] = (np.clip(vf, 0, h - 1) + 0.5) * UPSCALE - 0.5
        xs["xb"][rows] = (np.clip(ub, 0, half - 1) + half + 0.5) * UPSCALE - 0.5
        xs["yb"][rows] = (np.clip(vb, 0, h - 1) + 0.5) * UPSCALE - 0.5
        wgt = np.clip(0.5 + (tb - tf) / (2 * math.radians(BLEND_DEG)), 0, 1)   # доля переднего объектива
        m[rows] = np.round(wgt * 255).astype(np.uint8)
    for k, a in xs.items():
        out[k] = tmp / f"{k}.pgm"
        _pgm16(out[k], a)
    from PIL import Image
    out["mask"] = tmp / "mask.png"
    Image.fromarray(m).save(out["mask"])
    return out


def gains(c: Calib, frames: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Множители RGB для переднего и заднего объектива: одинаковая яркость на шве."""
    h, w = frames[0].shape[:2]
    half = w // 2
    d = _dirs(1024, 512)
    _, _, tf = _lens(d, c, False, h)
    _, _, tb = _lens(d, c, True, h)
    lat = np.arcsin(d[..., 1])
    band = (np.abs(tb - tf) < math.radians(4)) & (lat > math.radians(-55))
    D = d[band]
    uf, vf, _ = _lens(D, c, False, h)
    ub, vb, _ = _lens(D, c, True, h)
    fa, fb = [], []
    for img in frames:
        f32 = img.astype(np.float32)
        fa.append(_sample(f32[:, :half], uf, vf).mean(0))
        fb.append(_sample(f32[:, half:], ub, vb).mean(0))
    a, b = np.mean(fa, 0), np.mean(fb, 0)
    g = (a + b) / 2
    return np.clip(g / np.maximum(a, 1), 0.7, 1.4), np.clip(g / np.maximum(b, 1), 0.7, 1.4)


def _mixer(g: np.ndarray) -> str:
    return f"colorchannelmixer=rr={g[0]:.4f}:gg={g[1]:.4f}:bb={g[2]:.4f}"


def _encoder(args) -> list[str]:
    if args.vcodec == "hevc":
        enc = ["-c:v", "libx265", "-preset", args.preset, "-crf", str(args.crf if args.crf is not None else 22),
               "-tag:v", "hvc1", "-x265-params", "log-level=error"]
    else:
        enc = ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf if args.crf is not None else 20)]
    enc += ["-pix_fmt", "yuv420p"]
    if args.max_bitrate:
        r = int(args.max_bitrate * 1_000_000)
        enc += ["-maxrate", str(r), "-bufsize", str(2 * r)]
    return enc


def render(args, src: Source, maps: dict, gf, gb, out: Path, audio: bool = True) -> None:
    w, h = src.width, src.height
    graph = (f"[0:v]scale={w * UPSCALE}:{h * UPSCALE}:flags=bicubic,format=gbrp,split[s1][s2];"
             f"[s1][1:v][2:v]remap[f0];[s2][3:v][4:v]remap[b0];"
             f"[f0]{_mixer(gf)}[f];[b0]{_mixer(gb)}[b];[5:v]format=gbrp[m];"
             f"[b][f][m]maskedmerge,format=yuv420p[v]")
    cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-stats", "-y", "-i", str(src.path)]
    for k in ("xf", "yf", "xb", "yb"):
        cmd += ["-i", str(maps[k])]
    cmd += ["-loop", "1", "-i", str(maps["mask"]), "-filter_complex", graph, "-map", "[v]"]
    if src.has_audio and audio:
        cmd += ["-map", "0:a:0", "-c:a", "copy"]
    # даты и прочие метки исходника; moov в конце — пометка 360 дописывается без перезаписи файла
    # звук обрезается ровно по видео: иначе в серии рассинхрон копился бы от ролика к ролику
    dur = src.video_duration or src.duration
    cmd += ["-map_metadata", "0", "-t", f"{dur:.3f}"] + _encoder(args) + [
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", str(out)]
    proc = subprocess.run(cmd, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError("ffmpeg: " + " | ".join(err[-4:]))


# --- команда ---------------------------------------------------------------------------------

def _out_free(p: Path) -> Path:
    n, cand = 2, p
    while cand.exists():
        cand = p.with_name(f"{p.stem}_v{n}{p.suffix}")
        n += 1
    return cand


def _fmt(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def stitch360(args) -> int:
    from .config import VIDEO_EXTS
    from .pano import inject_360_tail
    from .tools import _copy_times, _expand_masks

    started = time.monotonic()
    files = [p for p in _expand_masks(args.masks) if p.suffix.lower() in VIDEO_EXTS
             and SUFFIX not in p.stem.casefold() and "_stitch" not in p.stem.casefold()]
    sources: list[Source] = []
    for p in files:
        try:
            s = _probe(args.ffprobe, p)
        except Exception as e:  # noqa: BLE001
            print(f"Пропуск {p.name}: не удалось прочитать ({e})")
            continue
        if s is None or abs(s.width - 2 * s.height) > 2:
            print(f"Пропуск {p.name}: не два круга 2:1 ({s.width}x{s.height})" if s else f"Пропуск {p.name}: нет видео")
            continue
        if _spherical(args.ffprobe, p):
            print(f"Пропуск {p.name}: уже сшит (есть пометка 360)")
            continue
        sources.append(s)
    if not sources:
        print("Несшитых роликов 360 не найдено", file=sys.stderr)
        return 2
    # порядок — по дате съёмки, затем по имени
    sources.sort(key=lambda s: (s.creation_time or "", s.path.name.casefold()))

    print(f"Роликов: {len(sources)}, всего {_fmt(sum(s.duration for s in sources))}")
    samples: dict[Path, list[np.ndarray]] = {}
    for s in sources:
        try:
            samples[s.path] = [_frame(args.ffmpeg, s, s.duration * k) for k in (0.2, 0.5, 0.8)]
        except Exception as e:  # noqa: BLE001
            print(f"Пропуск {s.path.name}: {e}")
            continue
        if not _looks_dual_fisheye(samples[s.path][1]):
            print(f"Пропуск {s.path.name}: в кадре не два круга «рыбий глаз»")
            del samples[s.path]
    sources = [s for s in sources if s.path in samples]
    if not sources:
        print("Несшитых роликов 360 не найдено", file=sys.stderr)
        return 2
    if len({(s.width, s.height) for s in sources}) > 1:
        print("Ролики разного размера: сшиваю только размер первого", file=sys.stderr)
        sources = [s for s in sources if (s.width, s.height) == (sources[0].width, sources[0].height)]

    calib = None
    if args.calibration and Path(args.calibration).is_file():
        calib = Calib(**json.loads(Path(args.calibration).read_text(encoding="utf-8")))
        print(f"Калибровка из файла {args.calibration}")
    else:
        pick = [f for s in sources[:3] for f in samples[s.path]][:6]
        print(f"Калибровка объективов по {len(pick)} кадрам...")
        calib = calibrate(pick)
        if args.calibration:
            Path(args.calibration).write_text(json.dumps(calib.__dict__, indent=2), encoding="utf-8")
            print(f"  сохранена в {args.calibration}")
    print(f"  угол обзора {calib.fov:.2f}°, поворот заднего объектива: рыскание {calib.yaw:+.2f}°, "
          f"наклон {calib.pitch:+.2f}°, крен {calib.roll:+.2f}°")
    if args.dry_run:
        for s in sources:
            print(f"  {s.creation_time or '?':>27}  {_fmt(s.duration)}  {s.path.name}")
        print("Режим --dry-run: ролики не сшивались")
        return 0

    w, h = sources[0].width, sources[0].height
    folder = sources[0].path.parent
    with tempfile.TemporaryDirectory(prefix="stitch_", dir=args.temp_dir) as td:
        tmp = Path(td)
        maps = build_maps(calib, w, h, tmp)
        done = []
        for i, s in enumerate(sources, 1):
            gf, gb = gains(calib, samples[s.path])
            if args.series:
                out = tmp / f"part{i:04d}.mp4"
            else:
                out = _out_free(s.path.with_name(f"{s.path.stem}{SUFFIX}.mp4"))
            print(f"[{i}/{len(sources)}] {s.path.name} ({_fmt(s.duration)}) → {out.name if not args.series else 'серия'}; "
                  f"яркость: передний ×{gf.mean():.2f}, задний ×{gb.mean():.2f}")
            part = out.with_name(out.stem + ".part.mp4")
            try:
                render(args, s, maps, gf, gb, part, audio=not args.series)
            except Exception as e:  # noqa: BLE001
                part.unlink(missing_ok=True)
                print(f"  ОШИБКА: {e}", file=sys.stderr)
                return 1
            os.replace(part, out)
            if not args.series:
                if not inject_360_tail(out):
                    print("  не удалось записать пометку 360", file=sys.stderr)
                _copy_times(s.path, out)
            done.append(out)
        if args.series:
            out = _out_free(folder / f"{folder.name}{SERIES_SUFFIX}.mp4")
            print(f"Склейка серии: {out.name}")
            # видео склеивается без перекодировки; звук каждого ролика — в PCM ровно по длине его
            # сшитого видео, так что рассинхрон не копится от ролика к ролику
            wavs = []
            for i, (s, p) in enumerate(zip(sources, done)):
                vd = _probe(args.ffprobe, p).video_duration
                wav = tmp / f"a{i:04d}.wav"
                if s.has_audio:
                    cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(s.path),
                           "-map", "0:a:0", "-vn", "-af", f"aresample=48000,apad=whole_dur={vd:.6f},atrim=end={vd:.6f}"]
                else:
                    cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                           "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{vd:.6f}"]
                subprocess.run(cmd + ["-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(wav)], check=True)
                wavs.append(wav)
            (tmp / "v.txt").write_text("".join(f"file '{p.as_posix()}'\n" for p in done), encoding="utf-8")
            (tmp / "a.txt").write_text("".join(f"file '{p.as_posix()}'\n" for p in wavs), encoding="utf-8")
            part = out.with_name(out.stem + ".part.mp4")
            proc = subprocess.run([args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                                   "-f", "concat", "-safe", "0", "-i", str(tmp / "v.txt"),
                                   "-f", "concat", "-safe", "0", "-i", str(tmp / "a.txt"),
                                   "-i", str(sources[0].path), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                                   "-c:a", "aac", "-b:a", "192k", "-map_metadata", "2", str(part)],
                                  stderr=subprocess.PIPE)
            if proc.returncode != 0:
                part.unlink(missing_ok=True)
                print("ОШИБКА склейки: " + proc.stderr.decode("utf-8", errors="replace")[-400:], file=sys.stderr)
                return 1
            os.replace(part, out)
            if not inject_360_tail(out):
                print("  не удалось записать пометку 360", file=sys.stderr)
            _copy_times(sources[0].path, out)
            done = [out]
    for p in done:
        print(f"Готово: {p}")
    print(f"Время работы: {_fmt(time.monotonic() - started)}")
    return 0


def add_parser(sub, seconds) -> None:
    p = sub.add_parser(
        "stitch360", help="сшить видео 360 из двух полусфер (Samsung Gear 360) в панораму 2:1",
        description="Сшивает ролики камеры 360 с двумя объективами «рыбий глаз» (кадр — два круга рядом) "
                    "в равнопромежуточную панораму 2:1 с пометкой 360. Результат — <имя>_stitch.mp4 рядом "
                    "с исходником, с --series — один ролик <каталог>_stitch_series.mp4. Дата съёмки в "
                    "метаданных и даты файла сохраняются. Уже сшитые файлы (с пометкой 360 или _Stitch, "
                    "_stitch в имени) пропускаются.")
    p.add_argument("masks", nargs="+", metavar="МАСКА", help="файлы или маски, например E:\\360\\*.MP4")
    p.add_argument("--series", action="store_true",
                   help="склеить все ролики (по времени съёмки) в один <каталог>_stitch_series.mp4 "
                        "вместо отдельных файлов")
    p.add_argument("--calibration", metavar="ФАЙЛ.json",
                   help="файл калибровки объективов: если есть — берётся из него, иначе считается и "
                        "сохраняется в него (удобно для одной камеры на много каталогов)")
    p.add_argument("--vcodec", choices=["h264", "hevc"], default="h264", help="кодек результата (h264)")
    p.add_argument("--crf", type=int, help="качество (h264: 20, hevc: 22)")
    p.add_argument("--preset", default="medium", help="скорость кодировщика (medium; быстрее — veryfast)")
    p.add_argument("--max-bitrate", type=float, metavar="МБИТ", help="потолок битрейта, Мбит/с")
    p.add_argument("--temp-dir", type=Path, help="каталог для временных файлов")
    p.add_argument("--dry-run", action="store_true", help="только калибровка и список файлов")
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--ffprobe", default="ffprobe")
    p.set_defaults(func=stitch360)
