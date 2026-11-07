"""Склейка сегментов, фоновая музыка и итоговое сведение."""
from __future__ import annotations

import logging
import random
from pathlib import Path

from .config import MUSIC_EXTS, Settings
from .planner import Base, Segment
from .probe import probe_duration
from .render import run

log = logging.getLogger("avtokino")


def _concat_list(paths: list[Path], out: Path) -> Path:
    lines = []
    for p in paths:
        escaped = p.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def concat_audio(segments: list[Segment], s: Settings, tmp: Path) -> Path:
    lst = _concat_list([seg.audio_path for seg in segments], tmp / "audio.txt")
    out = tmp / "main.wav"
    run([s.ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(out)])
    return out


def music_files(music_dir: Path) -> list[Path]:
    files = [p for p in music_dir.iterdir() if p.is_file() and p.suffix.lower() in MUSIC_EXTS]
    return sorted(files, key=lambda p: p.name.casefold())


def build_music(total: float, s: Settings, tmp: Path) -> Path | None:
    tracks = music_files(s.music_dir)
    if not tracks:
        log.warning("В каталоге музыки нет подходящих файлов: %s", s.music_dir)
        return None
    if s.music_shuffle:
        random.shuffle(tracks)
        log.info("Музыка в случайном порядке: %s", ", ".join(t.name for t in tracks))
    xf = s.music_crossfade
    usable: list[tuple[Path, Path, float]] = []
    # Треков, нужных для ролика, может быть меньше, чем в каталоге; нормализуем по мере надобности.
    playlist: list[tuple[Path, float]] = []
    length = 0.0
    i = 0
    while length < total:
        if i < len(tracks):
            t = tracks[i]
            wav = tmp / f"music{i:03d}.wav"
            try:
                run([s.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(t), "-vn",
                     "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample={s.arate}",
                     "-ac", str(s.achannels), "-ar", str(s.arate), "-c:a", "pcm_s16le", str(wav)])
                dur = probe_duration(s.ffprobe, wav)
            except Exception as e:  # noqa: BLE001
                log.warning("  музыка: %s пропущен (%s)", t.name, e)
                dur = 0.0
            if dur > 2 * xf + 1:
                usable.append((t, wav, dur))
        elif not usable:
            log.warning("Нет пригодных музыкальных файлов в %s", s.music_dir)
            return None
        else:
            t, wav, dur = usable[(i - len(tracks)) % len(usable)]
        i += 1
        if dur <= 2 * xf + 1:
            continue
        playlist.append((wav, dur))
        log.info("  музыка: %s (%.1f с)", t.name, dur)
        length += dur - (xf if len(playlist) > 1 else 0)

    out = tmp / "music.wav"
    cmd = [s.ffmpeg, "-hide_banner", "-nostdin", "-y"]
    for wav, _ in playlist:
        cmd += ["-i", str(wav)]
    parts, last = [], "[0:a]"
    for k in range(1, len(playlist)):
        d = min(xf, playlist[k][1] / 2, playlist[k - 1][1] / 2)
        parts.append(f"{last}[{k}:a]acrossfade=d={d:.3f}:c1=tri:c2=tri[x{k}]")
        last = f"[x{k}]"
    fade = min(s.music_fadeout, total / 2)
    parts.append(f"{last}atrim=end={total:.6f},afade=t=out:st={total - fade:.6f}:d={fade:.3f}"
                 f",volume={s.music_volume}[m]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[m]", "-c:a", "pcm_s16le", str(out)]
    run(cmd)
    return out


def mix_audio(main: Path, music: Path | None, s: Settings, tmp: Path) -> Path:
    out = tmp / "audio.m4a"
    enc = ["-c:a", s.acodec, "-b:a", s.abitrate, "-ar", str(s.arate), "-ac", str(s.achannels)]
    if music is None:
        run([s.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(main),
             "-af", f"volume={s.volume},alimiter=limit=0.97"] + enc + [str(out)])
        return out
    if s.ducking:
        graph = (f"[0:a]volume={s.volume},asplit[m1][m2];"
                 "[1:a][m2]sidechaincompress=threshold=0.02:ratio=6:attack=50:release=800[duck];"
                 "[m1][duck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.97[a]")
    else:
        graph = (f"[0:a]volume={s.volume}[m1];"
                 "[m1][1:a]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.97[a]")
    run([s.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(main), "-i", str(music),
         "-filter_complex", graph, "-map", "[a]"] + enc + [str(out)])
    return out


def mux(segments: list[Segment], audio: Path, base: Base, s: Settings, tmp: Path, out: Path) -> None:
    lst = _concat_list([seg.video_path for seg in segments], tmp / "video.txt")
    tag = ["-tag:v", "hvc1"] if base.vcodec == "hevc" else []
    run([s.ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy"] + tag +
        ["-movflags", "+faststart", str(out)])
