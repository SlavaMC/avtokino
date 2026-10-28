"""Рендер отдельных сегментов во временный каталог.

Видео каждого сегмента пишется в MPEG-TS: параметры кодека хранятся в самом
потоке, поэтому сегменты с разных камер склеиваются без перекодировки.
Звук каждого сегмента пишется отдельно в WAV ровно той же длительности.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import ENCODERS, Settings
from .planner import Base, Segment
from .probe import load_photo, probe_duration

log = logging.getLogger("avtokino")

_BT709 = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")


class RenderError(RuntimeError):
    pass


def run(cmd: list[str], stdin=None) -> None:
    log.debug("$ %s", subprocess.list2cmdline(cmd))
    proc = subprocess.run(cmd, input=stdin, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-5:]
        raise RenderError("ffmpeg: " + " | ".join(tail))


def has_filter(ffmpeg: str, name: str) -> bool:
    out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True).stdout.decode(errors="replace")
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


def encoder_args(base: Base, s: Settings) -> list[str]:
    args = ["-c:v", ENCODERS[base.vcodec], "-preset", s.preset, "-crf", str(base.crf),
            "-pix_fmt", "yuv420p", "-g", str(max(1, round(float(base.fps) * 2)))]
    if base.vcodec == "h264":
        args += ["-profile:v", "high"]
    else:
        args += ["-x265-params", "log-level=error"]
    if base.vbitrate:
        args += ["-maxrate", str(base.vbitrate), "-bufsize", str(base.vbitrate * 2)]
    return args + _BT709


def _fade(duration: float, s: Settings, audio: bool = False) -> str:
    if s.transition != "fade":
        return ""
    t = min(s.transition_duration, duration / 3)
    name = "afade" if audio else "fade"
    return f",{name}=t=in:st=0:d={t:.3f},{name}=t=out:st={duration - t:.3f}:d={t:.3f}"


def render_video(seg: Segment, base: Base, s: Settings, out: Path, tonemap_ok: bool) -> None:
    src = seg.item.path
    cmd = [s.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(src),
           "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1"]
    if seg.kind == "copy":
        cmd += ["-c:v", "copy"]
    else:
        v = seg.item.info
        chain = []
        if v.field_order not in ("progressive", "unknown"):
            chain.append("bwdif")
        if v.hdr:
            if tonemap_ok:
                chain.append(TONEMAP)
            else:
                log.warning("%s: HDR, но фильтр zscale недоступен, цвета могут быть блёклыми", seg.item.rel)
        chain += [
            f"scale={base.width}:{base.height}:force_original_aspect_ratio=decrease:flags=lanczos"
            ":out_color_matrix=bt709:out_range=tv",
            f"pad={base.width}:{base.height}:(ow-iw)/2:(oh-ih)/2:color=black",
            "setsar=1",
            f"fps={base.fps.numerator}/{base.fps.denominator}",
            "format=yuv420p",
        ]
        vf = ",".join(chain) + _fade(seg.duration, s)
        cmd += ["-vf", vf] + encoder_args(base, s)
    cmd += ["-f", "mpegts", str(out)]
    run(cmd)


def _prepare_photo(img: Image.Image, base: Base, zoom: float) -> Image.Image:
    fit = min(base.width / img.width, base.height / img.height)
    size = (max(1, round(img.width * fit * zoom)), max(1, round(img.height * fit * zoom)))
    return img.resize(size, Image.LANCZOS)


def _frames(prepared: Image.Image, base: Base, zoom: float, n: int, zoom_in: bool):
    """Кадры с субпиксельным масштабированием: без «дрожания», как у zoompan."""
    W, H = base.width, base.height
    pw, ph = prepared.size
    for i in range(n):
        t = i / max(1, n - 1)
        z = 1 + (zoom - 1) * (t if zoom_in else 1 - t)
        k = zoom / z  # пикселей подготовленного изображения на пиксель кадра
        data = (k, 0, pw / 2 - k * W / 2, 0, k, ph / 2 - k * H / 2)
        yield prepared.transform((W, H), Image.AFFINE, data, resample=Image.BICUBIC,
                                 fillcolor=(0, 0, 0)).tobytes()


def _encode_frames(frames, n: int, base: Base, s: Settings, duration: float, out: Path) -> None:
    vf = f"scale=out_color_matrix=bt709:out_range=tv,format=yuv420p{_fade(duration, s)}"
    cmd = [s.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{base.width}x{base.height}",
           "-framerate", f"{base.fps.numerator}/{base.fps.denominator}", "-i", "-",
           "-vf", vf] + encoder_args(base, s) + ["-f", "mpegts", str(out)]
    log.debug("$ %s", subprocess.list2cmdline(cmd))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for frame in frames:
            proc.stdin.write(frame)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode("utf-8", errors="replace")
    if proc.wait() != 0:
        raise RenderError("ffmpeg: " + " | ".join(err.strip().splitlines()[-5:]))


def render_photo(seg: Segment, base: Base, s: Settings, out: Path) -> None:
    n = max(1, round(seg.duration * float(base.fps)))
    prepared = _prepare_photo(load_photo(seg.item.path), base, s.photo_zoom)
    _encode_frames(_frames(prepared, base, s.photo_zoom, n, seg.zoom_in), n, base, s, seg.duration, out)


_FONTS = ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
          "arial.ttf", "C:/Windows/Fonts/arial.ttf", "segoeui.ttf"]


def _font(size: int):
    for f in _FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_title(seg: Segment, base: Base, s: Settings, out: Path, lines: list[str]) -> None:
    img = Image.new("RGB", (base.width, base.height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    sizes = [base.height // 12, base.height // 20]
    fonts = [_font(sz) for sz in sizes]
    heights = [draw.textbbox((0, 0), ln, font=f)[3] for ln, f in zip(lines, fonts)]
    y = (base.height - sum(heights) - base.height // 30 * (len(lines) - 1)) / 2
    for ln, f, h in zip(lines, fonts, heights):
        w = draw.textlength(ln, font=f)
        draw.text(((base.width - w) / 2, y), ln, font=f, fill=(255, 255, 255))
        y += h + base.height // 30
    n = max(1, round(seg.duration * float(base.fps)))
    zoom = 1.03
    prepared = _prepare_photo(img, base, zoom)
    _encode_frames(_frames(prepared, base, zoom, n, True), n, base, s, seg.duration, out)


def render_audio(seg: Segment, s: Settings, out: Path) -> None:
    d = seg.real_duration
    fmt = ["-ac", str(s.achannels), "-ar", str(s.arate), "-c:a", "pcm_s16le"]
    if seg.item is not None and seg.item.kind == "video" and seg.item.info.has_audio:
        af = f"aresample={s.arate},apad=whole_dur={d:.6f},atrim=end={d:.6f}" + _fade(d, s, audio=True)
        cmd = [s.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(seg.item.path),
               "-map", "0:a:0", "-vn", "-af", af] + fmt + [str(out)]
    else:
        layout = "stereo" if s.achannels == 2 else "mono"
        cmd = [s.ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
               "-i", f"anullsrc=r={s.arate}:cl={layout}", "-t", f"{d:.6f}"] + fmt + [str(out)]
    run(cmd)


def render_segment(seg: Segment, base: Base, s: Settings, tmp: Path, tonemap_ok: bool,
                   title_lines: list[str]) -> Segment:
    vout = tmp / f"seg{seg.index:05d}.ts"
    if seg.kind in ("copy", "transcode"):
        render_video(seg, base, s, vout, tonemap_ok)
    elif seg.kind == "photo":
        render_photo(seg, base, s, vout)
    else:
        render_title(seg, base, s, vout, title_lines)
    seg.video_path = vout
    seg.real_duration = probe_duration(s.ffprobe, vout)
    aout = tmp / f"seg{seg.index:05d}.wav"
    render_audio(seg, s, aout)
    seg.audio_path = aout
    return seg
