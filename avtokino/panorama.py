"""Команда panorama: плоская панорама из нескольких кадров (JPEG или RAW) в один JPEG.

Шаги:
  1. кадры проявляются одинаково: RAW — в линейные 16 бит с балансом белого камеры, без автояркости;
     JPEG/PNG/TIFF переводятся из sRGB в линейный свет;
  2. по зонам перекрытия соседних кадров вычисляются виньетирование объектива (тёмные углы)
     и экспозиция каждого кадра (камера могла менять её по ходу серии) — и снимаются до склейки,
     иначе на однотонном небе видны полосы на стыках;
  3. склейка OpenCV (Stitcher PANORAMA): общая геометрия, шов по линии наименьшей разницы,
     многополосное смешивание;
  4. обрезка до наибольшего прямоугольника без пустых углов, цветокоррекция,
     JPEG с датой съёмки и камерой первого кадра; даты файла — как у первого кадра.
"""
from __future__ import annotations

import math
import os
import re
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

RAW_EXTS = {".orf", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".rw2", ".raf", ".dng",
            ".pef", ".srw", ".x3f", ".3fr", ".iiq", ".rwl", ".mrw", ".kdc", ".erf", ".mos"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".heic", ".heif"}
EST_SCALE = 0.25          # оценка виньетирования — на уменьшенных кадрах


def _need(mod: str, pip: str):
    try:
        return __import__(mod)
    except ImportError:
        print(f"Для команды panorama нужен пакет {pip}: pip install {pip}", file=sys.stderr)
        raise SystemExit(2)


# --- чтение кадров ------------------------------------------------------------------

def _srgb_to_linear(a: np.ndarray) -> np.ndarray:
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4).astype(np.float32)


def _linear_to_srgb(a: np.ndarray) -> np.ndarray:
    a = np.clip(a, 0, 1)
    return np.where(a <= 0.0031308, 12.92 * a, 1.055 * np.power(a, 1 / 2.4) - 0.055)


def load_linear(path: Path) -> np.ndarray:
    """Кадр в линейном свете, float32 RGB 0..1 (с учётом ориентации)."""
    if path.suffix.lower() in RAW_EXTS:
        rawpy = _need("rawpy", "rawpy")
        with rawpy.imread(str(path)) as r:
            rgb = r.postprocess(use_camera_wb=True, no_auto_bright=True, gamma=(1, 1), output_bps=16)
        return rgb.astype(np.float32) / 65535
    from .probe import load_photo
    img = load_photo(path)                       # EXIF-ориентация, RGB
    a = np.asarray(img)
    scale = 65535 if a.dtype == np.uint16 else 255
    return _srgb_to_linear(a.astype(np.float32) / scale)


# --- дата и камера из RAW (TIFF-подобные: ORF, CR2, NEF, ARW, DNG, PEF, RW2) -----------------

def _tiff_tags(path: Path) -> dict:
    """DateTimeOriginal, Make, Model и смещение времени из IFD0 и EXIF IFD."""
    out: dict = {}
    try:
        with open(path, "rb") as f:
            data = f.read(4 << 20)
    except OSError:
        return out
    if data[:2] == b"II":
        e = "<"
    elif data[:2] == b"MM":
        e = ">"
    else:
        return out

    def ifd(off: int):
        if off <= 0 or off + 2 > len(data):
            return {}
        n = struct.unpack(e + "H", data[off:off + 2])[0]
        tags = {}
        for i in range(min(n, 500)):
            p = off + 2 + i * 12
            if p + 12 > len(data):
                break
            tag, typ, cnt = struct.unpack(e + "HHI", data[p:p + 8])
            if typ == 2:                                       # ASCII
                vo = p + 8 if cnt <= 4 else struct.unpack(e + "I", data[p + 8:p + 12])[0]
                tags[tag] = data[vo:vo + cnt].split(b"\0")[0].decode("ascii", "replace").strip()
            elif typ in (4, 13):                               # LONG / IFD
                tags[tag] = struct.unpack(e + "I", data[p + 8:p + 12])[0]
        return tags

    first = struct.unpack(e + "I", data[4:8])[0]
    t0 = ifd(first)
    tex = ifd(t0.get(0x8769, 0))
    for k, name in ((0x010F, "make"), (0x0110, "model"), (0x0132, "datetime")):
        if k in t0:
            out[name] = t0[k]
    for k, name in ((0x9003, "original"), (0x9004, "digitized"), (0x9011, "offset")):
        if k in tex:
            out[name] = tex[k]
    return out


def source_exif(path: Path):
    """EXIF для результата: дата съёмки и камера первого кадра (Pillow Image.Exif)."""
    from PIL import Image
    exif = Image.Exif()
    if path.suffix.lower() in RAW_EXTS:
        t = _tiff_tags(path)
        dt = t.get("original") or t.get("datetime")
        if not dt:
            dt = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y:%m:%d %H:%M:%S")
        make, model, off = t.get("make"), t.get("model"), t.get("offset")
    else:
        try:
            with Image.open(path) as im:
                src = im.getexif()
                ex = src.get_ifd(0x8769)
                dt = ex.get(0x9003) or src.get(0x0132)
                make, model, off = src.get(0x010F), src.get(0x0110), ex.get(0x9011)
        except Exception:  # noqa: BLE001
            dt = make = model = off = None
        if not dt:
            dt = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y:%m:%d %H:%M:%S")
    exif[0x0132] = dt
    if make:
        exif[0x010F] = make
    if model:
        exif[0x0110] = model
    exif[0x0131] = "avtokino panorama"
    ex = exif.get_ifd(0x8769)
    ex[0x9003] = dt
    ex[0x9004] = dt
    if off:
        ex[0x9011] = off
    return exif


# --- виньетирование и экспозиция ------------------------------------------------------------

def estimate_vignetting(small: list[np.ndarray], log=print) -> tuple[np.ndarray, np.ndarray]:
    """По перекрытиям всех пар кадров: коэффициенты log V(r) = c1 r² + c2 r⁴ + c3 r⁶ (r — от центра
    до угла = 1) и экспозиции кадров (первый = 1)."""
    cv2 = _need("cv2", "opencv-python-headless")
    n = len(small)
    h, w = small[0].shape[:2]
    cx, cy, R = w / 2, h / 2, math.hypot(w / 2, h / 2)
    gray = [(np.clip(_linear_to_srgb(i).mean(2), 0, 1) * 255).astype(np.uint8) for i in small]
    sift = cv2.SIFT_create(3000)
    kd = [sift.detectAndCompute(g, None) for g in gray]
    bf = cv2.BFMatcher()
    rows = []
    blur = [cv2.blur(i, (5, 5)) for i in small]
    lap = [np.abs(cv2.Laplacian(cv2.blur(i.mean(2), (5, 5)), cv2.CV_32F)) for i in small]
    pairs = 0
    for a in range(n):
        for b in range(a + 1, n):
            if kd[a][1] is None or kd[b][1] is None:
                continue
            m = bf.knnMatch(kd[a][1], kd[b][1], k=2)
            good = [x for x, *y in m if y and x.distance < 0.7 * y[0].distance]
            if len(good) < 20:
                continue
            H, mask = cv2.findHomography(np.float32([kd[a][0][g.queryIdx].pt for g in good]),
                                         np.float32([kd[b][0][g.trainIdx].pt for g in good]), cv2.RANSAC, 2)
            if H is None or mask.sum() < 20:
                continue
            ys, xs = np.mgrid[5:h - 5:3, 5:w - 5:3]
            P = np.stack([xs.ravel(), ys.ravel(), np.ones(xs.size)])
            Q = H @ P
            Q = Q[:2] / Q[2]
            ok = (Q[0] > 5) & (Q[0] < w - 5) & (Q[1] > 5) & (Q[1] < h - 5)
            if ok.sum() < 500:
                continue
            x1, y1 = P[0][ok].astype(int), P[1][ok].astype(int)
            x2, y2 = Q[0][ok], Q[1][ok]
            ia = blur[a][y1, x1].mean(1)
            ib = blur[b][y2.astype(int), x2.astype(int)].mean(1)
            sel = (lap[a][y1, x1] < 0.004) & (ia > 0.01) & (ib > 0.01) & (ia < 0.9) & (ib < 0.9)
            if sel.sum() < 200:
                continue
            r1 = np.hypot(x1 - cx, y1 - cy)[sel] / R
            r2 = np.hypot(x2 - cx, y2 - cy)[sel] / R
            d = np.log(ia[sel]) - np.log(ib[sel])
            rows.append(np.stack([np.full(d.size, a), np.full(d.size, b), r1, r2, d], 1))
            pairs += 1
    if not rows:
        log("  перекрытий для оценки виньетирования не нашлось — без поправки")
        return np.zeros(3), np.ones(n)
    rows = np.concatenate(rows)
    M = np.zeros((len(rows), 3 + n - 1))
    for j, p in enumerate((2, 4, 6)):
        M[:, j] = rows[:, 2] ** p - rows[:, 3] ** p
    a_idx, b_idx = rows[:, 0].astype(int), rows[:, 1].astype(int)
    for i in range(1, n):
        M[a_idx == i, 3 + i - 1] += 1
        M[b_idx == i, 3 + i - 1] -= 1
    y = rows[:, 4]
    sol = np.linalg.lstsq(M, y, rcond=None)[0]
    for _ in range(2):                               # без выбросов (движущиеся предметы, блики)
        res = np.abs(M @ sol - y)
        keep = res < 3 * np.median(res) + 1e-6
        sol = np.linalg.lstsq(M[keep], y[keep], rcond=None)[0]
    c = sol[:3]
    if np.exp(c.sum()) > 1.05 or np.exp(c.sum()) < 0.3:   # неправдоподобно — не трогаем
        log("  виньетирование не определилось надёжно — без поправки")
        c = np.zeros(3)
    ex = np.exp(np.r_[0, sol[3:]])
    log(f"  пар с перекрытием: {pairs}; углы кадра темнее центра: ×{np.exp(c.sum()):.2f}; "
        f"экспозиция кадров: " + " ".join(f"{e:.2f}" for e in ex))
    return c, ex


def _vignette(h: int, w: int, c: np.ndarray) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    rr = ((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / ((w / 2) ** 2 + (h / 2) ** 2)
    return np.exp(c[0] * rr + c[1] * rr ** 2 + c[2] * rr ** 3).astype(np.float32)


# --- обрезка и цвет ---------------------------------------------------------------------------

def inscribed_rect(img: np.ndarray, k: int = 8) -> tuple[int, int, int, int]:
    """Наибольший прямоугольник внутри заполненной области (x0, y0, x1, y1)."""
    cv2 = _need("cv2", "opencv-python-headless")
    m = (img.max(2) > 0).astype(np.uint8)
    m = cv2.erode(m, np.ones((5, 5), np.uint8))
    ms = cv2.resize(m, (max(1, m.shape[1] // k), max(1, m.shape[0] // k)), interpolation=cv2.INTER_AREA) == 1
    h, w = ms.shape
    heights = np.zeros(w, int)
    best = (0, 0, 0, w, h)
    for y in range(h):
        heights = np.where(ms[y], heights + 1, 0)
        stack: list[tuple[int, int]] = []
        for x in range(w + 1):
            cur = heights[x] if x < w else 0
            start = x
            while stack and stack[-1][1] >= cur:
                s0, hh = stack.pop()
                area = hh * (x - s0)
                if area > best[0]:
                    best = (area, s0, y - hh + 1, x - s0, hh)
                start = s0
            stack.append((start, cur))
    _, x0, y0, bw, bh = best
    H, W = img.shape[:2]
    return (min(W, (x0 + 1) * k), min(H, (y0 + 1) * k),
            max(0, (x0 + bw - 1) * k), max(0, (y0 + bh - 1) * k))


def color_correct(bgr: np.ndarray, brightness: float = 1.0, contrast: float = 1.0,
                  saturation: float = 1.0) -> np.ndarray:
    """Автоматическая цветокоррекция: уровни, мягкий баланс белого, тени, контраст, насыщенность.
    brightness / contrast / saturation — множители силы (1 — по умолчанию)."""
    cv2 = _need("cv2", "opencv-python-headless")
    c = bgr.astype(np.float32) / 255
    wts = np.float32([0.114, 0.587, 0.299])
    lum = c @ wts
    lo, hi = np.percentile(lum, 0.05), np.percentile(lum, 99.8)
    c = np.clip((c - lo) / max(hi - lo, 1e-3), 0, 1)
    # баланс белого: половина поправки «серого мира» по средним тонам
    lum = c @ wts
    mid = (lum > np.percentile(lum, 20)) & (lum < np.percentile(lum, 80))
    g = c[mid].mean(0)
    c = np.clip(c * (g.mean() / np.maximum(g, 1e-3)) ** 0.5, 0, 1)
    # тени и средние тона
    L = c @ wts
    t = np.power(L, 1 - 0.18 * brightness)
    t = t + 0.05 * brightness * np.sin(np.pi * t) * (1 - t)
    # S-кривая контраста вокруг середины
    k = 0.35 * contrast
    t = np.clip(t + k * (t - 0.5) * (1 - np.abs(2 * t - 1)), 0, 1)
    c = np.clip(c * (t / np.maximum(L, 1e-4))[..., None], 0, 1)
    lab = cv2.cvtColor((c * 255 + 0.5).astype(np.uint8), cv2.COLOR_BGR2LAB)
    cl = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 16)).apply(lab[..., 0])
    wcl = min(0.9, 0.55 * contrast)               # доля локального контраста
    lab[..., 0] = cv2.addWeighted(lab[..., 0], 1 - wcl, cl, wcl, 0)
    lab = lab.astype(np.float32)
    lab[..., 1:] = 128 + (lab[..., 1:] - 128) * (1 + 0.2 * saturation)
    return cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


# --- команда --------------------------------------------------------------------------------

def _natural_key(p: Path):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", p.name)]


def output_name(files: list[Path]) -> Path:
    """P6070226 … P6070232 -> P6070226-232_pano.jpg (у последнего — только отличающийся хвост)."""
    a, b = files[0].stem, files[-1].stem
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    i = min(i, max(0, len(b) - 3))          # хвост не короче трёх знаков: 226-232, а не 226-32
    tail = b[i:] if i > 0 else b
    base = f"{a}-{tail}_pano" if a != b else f"{a}_pano"
    out = files[0].with_name(base + ".jpg")
    n = 2
    while out.exists():
        out = files[0].with_name(f"{base}_v{n}.jpg")
        n += 1
    return out


def panorama(args) -> int:
    cv2 = _need("cv2", "opencv-python-headless")
    from PIL import Image

    from .tools import _copy_times, _expand_masks

    started = time.monotonic()
    files = [p for p in _expand_masks(args.files)
             if p.suffix.lower() in RAW_EXTS | IMAGE_EXTS and not p.stem.casefold().endswith("_pano")]
    files.sort(key=_natural_key)
    if len(files) < 2:
        print("Нужно хотя бы два кадра (JPEG или RAW)", file=sys.stderr)
        return 2
    out = Path(args.output) if args.output else output_name(files)
    print(f"Кадров: {len(files)}: {', '.join(p.name for p in files)}")
    print(f"Панорама: {out}")

    print("Проявка и оценка виньетирования...")
    small, sizes = [], set()
    for p in files:
        a = load_linear(p)
        sizes.add(a.shape[:2])
        small.append(cv2.resize(a, None, fx=EST_SCALE, fy=EST_SCALE, interpolation=cv2.INTER_AREA))
    if len(sizes) > 1:
        print("  кадры разного размера — без поправки виньетирования")
        vc, ex = np.zeros(3), np.ones(len(files))
    elif args.no_vignetting:
        vc, ex = np.zeros(3), np.ones(len(files))
    else:
        vc, ex = estimate_vignetting(small)
    allp = np.concatenate([s.reshape(-1) / e for s, e in zip(small, ex)])
    gain = 0.92 / max(np.percentile(allp, 99.7), 1e-4)
    del small

    frames = []
    V = None
    for p, e in zip(files, ex):
        a = load_linear(p)
        if not np.allclose(vc, 0):
            if V is None or V.shape != a.shape[:2]:
                V = _vignette(a.shape[0], a.shape[1], vc)
            a /= V[..., None]
        s = _linear_to_srgb(a * (gain / e))
        frames.append(cv2.cvtColor((s * 255 + 0.5).astype(np.uint8), cv2.COLOR_RGB2BGR))
        del a, s

    print("Склейка...")
    st = cv2.Stitcher_create(cv2.Stitcher_PANORAMA)
    st.setCompositingResol(-1)
    status, pano = st.stitch(frames)
    del frames
    if status != 0:
        why = {1: "мало общих точек между кадрами (нужно перекрытие примерно на треть кадра)",
               2: "не удалось вычислить положение кадров", 3: "не удалось согласовать положение камеры"}
        print(f"Не удалось склеить: {why.get(status, f'код {status}')}", file=sys.stderr)
        return 1
    x0, y0, x1, y1 = inscribed_rect(pano) if not args.no_crop else (0, 0, pano.shape[1], pano.shape[0])
    pano = pano[y0:y1, x0:x1]
    if not args.no_color:
        pano = color_correct(pano, args.brightness, args.contrast, args.saturation)
    print(f"  размер {pano.shape[1]}x{pano.shape[0]}")
    fh = min(h for h, _ in sizes)
    if fh and pano.shape[0] < 0.5 * fh:
        print("  ВНИМАНИЕ: панорама получилась узкой полосой — похоже, кадры сняты не с одной точки "
              "или с разным приближением; проверьте результат")
    img = Image.fromarray(cv2.cvtColor(pano, cv2.COLOR_BGR2RGB))
    img.save(out, "JPEG", quality=args.quality, subsampling=0, exif=source_exif(files[0]).tobytes())
    _copy_times(files[0], out)
    print(f"Готово: {out}")
    print(f"Время работы: {int(time.monotonic() - started)} с")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser(
        "panorama", help="склеить плоскую панораму из нескольких кадров (JPEG или RAW) в JPEG",
        description="Склеивает кадры, снятые с одной точки с поворотом камеры, в одну панораму без видимых "
                    "стыков: выравнивает экспозицию и виньетирование, обрезает пустые края, делает "
                    "цветокоррекцию. Понимает JPEG/PNG/TIFF и RAW (ORF, CR2, CR3, NEF, ARW, RW2, RAF, DNG и др.). "
                    "Результат — <первый>-<последний>_pano.jpg рядом с первым кадром, с датой съёмки и "
                    "датами файла первого кадра. Нужны пакеты: pip install rawpy opencv-python-headless")
    p.add_argument("files", nargs="+", metavar="ФАЙЛ",
                   help="кадры серии: имена файлов или маски, например \"P60702*.ORF\"")
    p.add_argument("-o", "--output", metavar="ФАЙЛ.jpg", help="имя результата (по умолчанию <первый>-<последний>_pano.jpg)")
    p.add_argument("--quality", type=int, default=95, help="качество JPEG (95)")
    p.add_argument("--brightness", type=float, default=1.0,
                   help="сила осветления теней и средних тонов (1.0; 0 — не осветлять)")
    p.add_argument("--contrast", type=float, default=1.0, help="сила контраста (1.0; 0 — без добавки)")
    p.add_argument("--saturation", type=float, default=1.0, help="добавка насыщенности (1.0 = +20%%; 0 — без)")
    p.add_argument("--no-color", action="store_true", help="без цветокоррекции")
    p.add_argument("--no-vignetting", action="store_true", help="не исправлять виньетирование и экспозицию кадров")
    p.add_argument("--no-crop", action="store_true", help="не обрезать неровные края")
    p.set_defaults(func=panorama)
