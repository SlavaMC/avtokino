import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from avtokino.dating import Evidence, estimate_offset, name_date, parse_time_fix, resolve
from avtokino.naming import is_gopro, output_pattern, parse_dir_name
from avtokino.planner import aspect_frame, ratio_str


class DirNameTest(unittest.TestCase):
    def test_formats(self):
        e = parse_dir_name("20240615_Wedding")
        self.assertEqual((e.start, e.end, e.name), (date(2024, 6, 15), date(2024, 6, 15), "Wedding"))
        e = parse_dir_name("20240615-17_Trip")
        self.assertEqual(e.end, date(2024, 6, 17))
        e = parse_dir_name("20240628-0702_Trip")
        self.assertEqual(e.end, date(2024, 7, 2))
        e = parse_dir_name("20241230-0102_NY")
        self.assertEqual(e.end, date(2025, 1, 2))
        self.assertTrue(parse_dir_name("202406XX_Summer").month_only)
        self.assertTrue(parse_dir_name("202406_Summer").month_only)
        self.assertIsNone(parse_dir_name("Misc").start)

    def test_caption_date(self):
        self.assertEqual(parse_dir_name("202408XX_Sea").caption_date(), "Август 2024")
        self.assertEqual(parse_dir_name("20240615_W").caption_date(), "15 июня 2024")
        self.assertEqual(parse_dir_name("20240615-17_W").caption_date(), "15–17 июня 2024")
        self.assertEqual(parse_dir_name("20140731-0803_W").caption_date(), "31 июля – 3 августа 2014")

    def test_period(self):
        b, e = parse_dir_name("20140731-0803_W").period(timedelta(hours=6))
        self.assertEqual((b, e), (datetime(2014, 7, 30, 18), datetime(2014, 8, 4, 6)))
        b, e = parse_dir_name("202412XX_W").period()
        self.assertEqual((b, e), (datetime(2024, 12, 1), datetime(2025, 1, 1)))
        self.assertIsNone(parse_dir_name("Misc").period())

    def test_gopro(self):
        for name in ("GOPR4739.MP4", "GP014739.MP4", "GH010123.MP4", "gx020456.mp4"):
            self.assertTrue(is_gopro(Path(name)), name)
        for name in ("GOPR47390.MP4", "DSC_0001.MP4", "00080.MTS"):
            self.assertFalse(is_gopro(Path(name)), name)

    def test_output_pattern(self):
        r = output_pattern("20240615_Wedding")
        self.assertTrue(r.match("20240615_Wedding"))
        self.assertTrue(r.match("20240615_Wedding_v3"))
        self.assertFalse(r.match("20240615_Wedding_raw"))


class DatingTest(unittest.TestCase):
    def test_name_patterns(self):
        self.assertEqual(name_date(Path("IMG_20240615_123456.jpg")).value, datetime(2024, 6, 15, 12, 34, 56))
        self.assertEqual(name_date(Path("2024-06-15 12.34.56.jpg")).value, datetime(2024, 6, 15, 12, 34, 56))
        ev = name_date(Path("IMG-20240615-WA0001.jpg"))
        self.assertFalse(ev.has_time)
        self.assertIsNone(name_date(Path("DSC_0001.jpg")))

    def test_time_fix(self):
        d = parse_time_fix("2016-01-04 23:02:42 -> 2025-06-20 09:48:00")
        self.assertEqual(datetime(2016, 1, 4, 23, 2, 42) + d, datetime(2025, 6, 20, 9, 48))
        self.assertEqual(parse_time_fix("2020-01-01 10:00->2019-12-31 10:00"), timedelta(days=-1))
        with self.assertRaises(ValueError):
            parse_time_fix("вчера -> сегодня")

    def test_offset_from_pair(self):
        meta = Evidence(datetime(2024, 6, 15, 9, 34, 50, tzinfo=timezone.utc), "utc", "ct")
        name = Evidence(datetime(2024, 6, 15, 12, 34, 56), "local", "name")
        off, votes = estimate_offset([(meta, name)])
        self.assertEqual(off, timedelta(hours=3))
        r = resolve(meta, name, meta, off)
        self.assertEqual(r.when, datetime(2024, 6, 15, 12, 34, 50))


class ExcludeTest(unittest.TestCase):
    def test_exclude(self):
        from avtokino.cli import is_excluded
        self.assertTrue(is_excluded("Mobile/Screenshots", ["screenshots"]))
        self.assertTrue(is_excluded("Mobile/Screenshots", ["Mobile/Screenshots"]))
        self.assertTrue(is_excluded("Mobile/Screenshots", ["Mobile\\Screenshots"]))
        self.assertTrue(is_excluded("Old_1", ["*_1"]))
        self.assertFalse(is_excluded("Mobile", ["Mobile/Screenshots"]))
        self.assertFalse(is_excluded("GoPro8/Screenshots", ["Mobile/Screenshots"]))


class ShiftTest(unittest.TestCase):
    def test_shift(self):
        from avtokino.cli import _shift, shift_for
        sh = [_shift("Olympus/*=10800"), _shift("DSC*.JPG = -3600.5")]
        self.assertEqual(sh[1], ("DSC*.JPG", -3600.5))
        self.assertEqual(shift_for("Olympus/P1010001.jpg", sh), ("Olympus/*", 10800))
        self.assertEqual(shift_for("Mobile/dsc_01.jpg", sh), ("DSC*.JPG", -3600.5))
        self.assertIsNone(shift_for("Mobile/IMG_1.jpg", sh))


class FrameTest(unittest.TestCase):
    def test_frame(self):
        self.assertEqual(aspect_frame(1920, 1080, 16, 9), (1920, 1080))
        self.assertEqual(aspect_frame(2704, 2028, 16, 9), (3606, 2028))
        self.assertEqual(aspect_frame(1920, 1080, 4, 3), (1920, 1440))
        self.assertEqual(aspect_frame(1080, 1920, 16, 9), (3414, 1920))
        self.assertEqual(ratio_str(2704, 2028), "4:3")
        self.assertEqual(ratio_str(3606, 2028), "≈16:9")



class DurationTest(unittest.TestCase):
    def test_formats(self):
        from avtokino.durations import hours, parse_duration
        self.assertEqual(parse_duration("90"), 90)
        self.assertEqual(parse_duration("1:30"), 90)
        self.assertEqual(parse_duration("01:02:03.5"), 3723.5)
        self.assertEqual(parse_duration("-0:30", signed=True), -30)
        self.assertEqual(hours("1:30"), 1.5)
        for bad in ("1:75", "-5", "x", "1:2:3:4"):
            with self.assertRaises(ValueError):
                parse_duration(bad)


class TrimNamesTest(unittest.TestCase):
    def test_names(self):
        from avtokino.tools import _mmss, _parse_range
        self.assertEqual(_mmss(10), "00m10")
        self.assertEqual(_mmss(75.5), "01m15.5")
        self.assertEqual(_mmss(3725), "62m05")
        self.assertEqual(_parse_range("1:05-1:20"), (65, 80))
        self.assertEqual(_parse_range("30-"), (30, None))
        self.assertEqual(_parse_range("0:30-end"), (30, None))


class MulticamTest(unittest.TestCase):
    def test_offset(self):
        from avtokino.multicam import parse_offset
        self.assertEqual(parse_offset("+3:07"), 187)
        self.assertEqual(parse_offset("-187.5"), -187.5)
        self.assertEqual(parse_offset("1:02:03"), 3723)

    def test_plan_alternates(self):
        from fractions import Fraction
        from types import SimpleNamespace
        from datetime import datetime
        from avtokino.multicam import Clip, _plan
        def clip(cam, start, dur):
            info = SimpleNamespace(duration=dur, has_audio=True)
            return Clip(cam, Path(f"{cam}{start}.mp4"), info, datetime(2024, 1, 1), "", start=start)
        a, b = clip("A", 0, 20), clip("B", 7, 20)
        pieces = _plan([a, b], 5, Fraction(25), None, False)
        self.assertEqual([(p.t0, p.t1, p.video.cam) for p in pieces],
                         [(0, 7, "A"), (7, 12, "B"), (12, 17, "A"), (17, 27, "B")])
        # граница переключения не должна «съезжать» из-за округления до кадра (59,94 кадр/с)
        odd = _plan([clip("B", 0, 168), clip("A", 43.47, 92)], 5, Fraction(60000, 1001), None, False)
        lens = [round(p.t1 - p.t0, 2) for p in odd[1:-2]]
        self.assertTrue(all(abs(x - 5) < 0.02 for x in lens), lens)
        self.assertEqual(odd[1].video.cam, "A")
        only = _plan([a, b], 5, Fraction(25), "A", True)
        self.assertEqual(only[0].t0, 7)
        self.assertTrue(all(p.audio is a for p in only))


if __name__ == "__main__":
    unittest.main()


class PanoAutoTest(unittest.TestCase):
    def test_pano_detection_and_bursts(self):
        from avtokino.config import Settings
        from avtokino.planner import Item, is_pano, photo_durations
        from avtokino.probe import PhotoInfo

        s = Settings(workdir=Path("."))
        when = datetime(2018, 2, 13)
        pano = Item(Path("p.jpg"), "p.jpg", "photo", PhotoInfo(Path("p.jpg"), 7776, 3888, spherical=True), when, "")
        wide = Item(Path("w.jpg"), "w.jpg", "photo", PhotoInfo(Path("w.jpg"), 4000, 2000), when, "")
        odd = Item(Path("o.jpg"), "o.jpg", "photo", PhotoInfo(Path("o.jpg"), 4000, 3000, spherical=True), when, "")
        self.assertTrue(is_pano(pano, s))
        self.assertFalse(is_pano(wide, s))       # 2:1 без пометки 360
        self.assertFalse(is_pano(odd, s))        # пометка есть, но не 2:1
        s.pano = False
        self.assertFalse(is_pano(pano, s))
        s.pano = True
        # панорама разрывает серию: 3 + 3 фото не дают серию больше 5
        items = [wide] * 3 + [pano] + [wide] * 3
        self.assertEqual(photo_durations(items, s), [5.0] * 3 + [0.0] + [5.0] * 3)
