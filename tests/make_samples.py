"""Создаёт синтетический каталог события для проверки avtokino.

python tests/make_samples.py <куда>
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def video(path, size, fps, dur, creation, audio_rate=48000, text="", rotate=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d={dur}"]
    if audio_rate:
        args += ["-f", "lavfi", "-i", f"sine=f=440:r={audio_rate}:d={dur}", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-metadata", f"creation_time={creation}"]
    if rotate is None:
        ff(*args, str(path))
        return
    tmp = path.with_suffix(".tmp" + path.suffix)
    ff(*args, str(tmp))
    ff("-display_rotation", str(rotate), "-i", str(tmp), "-c", "copy",
       "-metadata", f"creation_time={creation}", str(path))
    tmp.unlink()


def photo(path, size, color, label, exif_dt=None, orientation=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, size[0] - 10, size[1] - 10], outline=(255, 255, 255), width=8)
    d.text((40, 40), label, fill=(255, 255, 255), font_size=max(20, size[1] // 10))
    exif = Image.Exif()
    if exif_dt:
        exif.get_ifd(0x8769)[36867] = exif_dt
    if orientation:
        exif[0x0112] = orientation
    img.save(path, exif=exif)


def main(root: Path):
    ev = root / "20240615_Test_Event"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "20240615_Test_Event.mp4").write_bytes(b"old output")  # должен быть пропущен
    video(ev / "VID_20240615_120000.mp4", "1920x1080", 30, 8, "2024-06-15T09:00:00Z")
    video(ev / "sub" / "clip720.mp4", "1280x720", 25, 6, "2024-06-15T09:05:00Z", audio_rate=44100)
    video(ev / "sub" / "deep" / "vertical.mov", "1920x1080", 30, 6, "2024-06-15T09:30:00Z",
          audio_rate=0, rotate=90)
    video(ev / "short.mp4", "1920x1080", 30, 3, "2024-06-15T09:01:00Z")
    for i in range(7):  # серия из 7 фото -> по 3 с
        photo(ev / "burst" / f"DSC_{i:04d}.jpg", (3000, 2000), (40 * i, 80, 160), f"burst {i}",
              f"2024:06:15 12:02:{i:02d}")
    photo(ev / "portrait.jpg", (2000, 3000), (150, 60, 60), "portrait", "2024:06:15 12:20:00")
    photo(ev / "rotated.jpg", (3000, 2000), (60, 150, 60), "orient 6", "2024:06:15 12:21:00", orientation=6)
    photo(ev / "IMG-20240615-WA0001.jpg", (1600, 900), (90, 90, 90), "whatsapp")
    music = root / "music"
    music.mkdir(exist_ok=True)
    ff("-f", "lavfi", "-i", "sine=f=330:d=20", "-c:a", "libmp3lame", str(music / "a_track.mp3"))
    ff("-f", "lavfi", "-i", "sine=f=523:d=20", "-c:a", "libmp3lame", str(music / "b_track.mp3"))
    print(ev)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
