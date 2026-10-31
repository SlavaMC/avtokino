import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from avtokino.dating import Evidence, estimate_offset, name_date, parse_time_fix, resolve
from avtokino.naming import is_gopro, output_pattern, parse_dir_name
from avtokino.planner import frame_16x9


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


class FrameTest(unittest.TestCase):
    def test_frame(self):
        self.assertEqual(frame_16x9(1920, 1080), (1920, 1080))
        self.assertEqual(frame_16x9(1080, 1920), (1920, 1080))
        self.assertEqual(frame_16x9(1440, 1080), (1920, 1080))
        self.assertEqual(frame_16x9(3840, 2160), (3840, 2160))


if __name__ == "__main__":
    unittest.main()
