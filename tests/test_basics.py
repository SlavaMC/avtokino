import os
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


class StitchTest(unittest.TestCase):
    def test_dual_fisheye_detection(self):
        import numpy as np
        from avtokino.stitch import _looks_dual_fisheye
        h, w = 400, 800
        yy, xx = np.mgrid[0:h, 0:w]
        img = np.zeros((h, w, 3), np.uint8)
        for cx in (200, 600):
            img[(xx - cx) ** 2 + (yy - 200) ** 2 < 195 ** 2] = 120
        self.assertTrue(_looks_dual_fisheye(img))
        self.assertFalse(_looks_dual_fisheye(np.full((h, w, 3), 120, np.uint8)))

    def test_gains_balance(self):
        import numpy as np
        from avtokino.stitch import Calib, gains
        img = np.full((400, 800, 3), 100, np.uint8)
        img[:, 400:] = 125                      # задний объектив светлее
        gf, gb = gains(Calib(195, 200, 200, 200, 200), [img])
        self.assertAlmostEqual(float(100 * gf[0]), float(125 * gb[0]), delta=1)


class PanoramaTest(unittest.TestCase):
    def test_output_name(self):
        from avtokino.panorama import output_name
        files = [Path("/nonexistent/P6070226.ORF"), Path("/nonexistent/P6070232.ORF")]
        self.assertEqual(output_name(files).name, "P6070226-232_pano.jpg")
        files = [Path("/nonexistent/IMG_0998.jpg"), Path("/nonexistent/IMG_1003.jpg")]
        self.assertEqual(output_name(files).name, "IMG_0998-1003_pano.jpg")

    def test_inscribed_rect(self):
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("нет opencv")
        import numpy as np
        from avtokino.panorama import inscribed_rect
        img = np.zeros((400, 800, 3), np.uint8)
        img[50:350, 100:700] = 200
        x0, y0, x1, y1 = inscribed_rect(img)
        self.assertTrue(100 <= x0 < 130 and 50 <= y0 < 80 and 670 < x1 <= 700 and 320 < y1 <= 350)


class TranscribeTest(unittest.TestCase):
    def _seg(self, words):
        from types import SimpleNamespace as N
        ws, t = [], 0.0
        for w in words:
            ws.append(N(word=" " + w, start=t, end=t + 0.4))
            t += 0.5
        return N(start=0.0, end=t, text=" ".join(words), words=ws)

    def test_split_and_formats(self):
        from avtokino.transcribe import split_lines, to_dated, to_srt
        seg = self._seg("Живи долго, интересно, как говорится, что тебе не нравится? Ну а мы тебя будем хвалить.".split())
        lines = split_lines([seg])
        self.assertEqual(lines[0].text, "Живи долго, интересно, как говорится, что тебе не нравится?")
        self.assertEqual(lines[1].text, "Ну а мы тебя будем хвалить.")
        self.assertIn("00:00:00,000 --> ", to_srt(lines))
        dated = to_dated(lines, datetime(2026, 11, 13, 18, 0, 0)).splitlines()
        self.assertTrue(dated[0].startswith("2026-11-13 18:00:00: Живи"))
        self.assertTrue(dated[1].startswith("2026-11-13 18:00:04: Ну"))

    def test_long_line_breaks_after_comma(self):
        from avtokino.transcribe import MAX_CHARS, split_lines
        words = ("раз два три четыре пять шесть, семь восемь девять десять одиннадцать двенадцать "
                 "тринадцать четырнадцать пятнадцать шестнадцать семнадцать").split()
        lines = split_lines([self._seg(words)])
        self.assertTrue(all(len(ln.text) <= MAX_CHARS for ln in lines))
        self.assertTrue(lines[0].text.endswith(","))


class SysProxyTest(unittest.TestCase):
    def test_parse(self):
        from avtokino.sysproxy import parse_proxy_list, bypass_to_no_proxy
        self.assertEqual(parse_proxy_list("10.0.0.1:3128"), "http://10.0.0.1:3128")
        self.assertEqual(parse_proxy_list("http=a:1;https=b:2"), "http://b:2")
        self.assertEqual(parse_proxy_list("http=a:1"), "http://a:1")
        self.assertEqual(parse_proxy_list("PROXY p:8080; DIRECT"), "http://p:8080")
        self.assertIsNone(parse_proxy_list("DIRECT"))
        self.assertEqual(bypass_to_no_proxy("<local>;*.corp.ru;intra"),
                         "localhost,127.0.0.1,::1,.corp.ru,intra")

    def test_apply(self):
        from unittest import mock
        from avtokino import sysproxy
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("--proxy", sysproxy.apply("p:3128"))
            self.assertEqual(os.environ["HTTPS_PROXY"], "http://p:3128")
            self.assertEqual(sysproxy.apply("off"), "")
            self.assertNotIn("HTTPS_PROXY", os.environ)
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://x:1"}, clear=True):
            self.assertIn("переменная", sysproxy.apply())


class HallucinationTest(unittest.TestCase):
    def test_filter(self):
        from types import SimpleNamespace as N
        from avtokino.transcribe import is_hallucination as h
        seg = lambda t, ns=0.1, lp=-0.3: N(text=t, no_speech_prob=ns, avg_logprob=lp)
        for t in ("Субтитры создавал DimaTorzok", " Субтитры делал DimaTorzok", "Продолжение следует...", "[Музыка]"):
            self.assertTrue(h(seg(t)), t)
        for t in ("Ну что, поздравляем папу!", "Спасибо за просмотр, а теперь к столу", "Мы сделали субтитры"):
            self.assertFalse(h(seg(t)), t)
        self.assertTrue(h(seg("Ага", ns=0.9, lp=-1.5)))
