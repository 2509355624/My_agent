"""漫画工作台水印：独立目录、原图只读、覆盖写、相对坐标（2026-11-21）。

用户作品被搬运 → 在带气泡成品图上叠自己的平台水印，产出到 watermark/，
绝不动 raw/ 与 final/。这里测纯函数与路径方法，API 路由做源码级断言。
"""
import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from app.features.comic import watermark as wm  # noqa: E402
from app.features.comic import comic  # noqa: E402


def _md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


class WatermarkStampTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "04_final.png")
        Image.new("RGB", (600, 900), (180, 180, 180)).save(self.src)

    def test_source_never_changes(self):
        before = _md5(self.src)
        wm.stamp(self.src, os.path.join(self.d, "04_dy233.png"),
                 [{"text": "dy：233", "x": 0.5, "y": 0.9}])
        self.assertEqual(_md5(self.src), before)

    def test_output_exists_same_size(self):
        dst = os.path.join(self.d, "04_dy233.png")
        n = wm.stamp(self.src, dst, [{"text": "dy：233", "x": 0.5, "y": 0.9}])
        self.assertEqual(n, 1)
        self.assertTrue(os.path.isfile(dst))
        self.assertEqual(Image.open(dst).size, (600, 900))

    def test_overwrite_same_target(self):
        dst = os.path.join(self.d, "04_dy233.png")
        wm.stamp(self.src, dst, [{"text": "dy：233", "x": 0.5, "y": 0.9}])
        first = _md5(dst)
        wm.stamp(self.src, dst, [{"text": "ks：我是233", "x": 0.3, "y": 0.2}])
        self.assertNotEqual(_md5(dst), first)   # 新内容确实覆盖旧文件

    def test_multiple_marks_all_stamped(self):
        dst = os.path.join(self.d, "04_multi.png")
        marks = [{"text": "dy：233", "x": 0.2, "y": 0.2},
                 {"text": "ks：我是233", "x": 0.8, "y": 0.8}]
        self.assertEqual(wm.stamp(self.src, dst, marks), 2)

    def test_relative_coords_scale_to_any_size(self):
        # 同一套坐标套到不同尺寸的图，都落在相对同一位置（水印确实画上了）。
        small = os.path.join(self.d, "small.png")
        big = os.path.join(self.d, "big.png")
        Image.new("RGB", (400, 600), (200, 200, 200)).save(small)
        Image.new("RGB", (1000, 1500), (200, 200, 200)).save(big)
        marks = [{"text": "dy233", "x": 0.5, "y": 0.5}]
        wm.stamp(small, small + ".wm.png", marks)
        wm.stamp(big, big + ".wm.png", marks)
        with Image.open(small + ".wm.png") as im:
            self.assertEqual(im.size, (400, 600))
        with Image.open(big + ".wm.png") as im:
            self.assertEqual(im.size, (1000, 1500))

    def test_empty_marks_still_copies(self):
        dst = os.path.join(self.d, "empty.png")
        self.assertEqual(wm.stamp(self.src, dst, []), 0)
        self.assertTrue(os.path.isfile(dst))

    def test_no_tmp_left_behind(self):
        wm.stamp(self.src, os.path.join(self.d, "04_wm.png"),
                 [{"text": "dy233", "x": 0.5, "y": 0.5}])
        self.assertFalse([f for f in os.listdir(self.d) if f.startswith(".wm_")])


class NormalizeTest(unittest.TestCase):
    def test_drops_empty_and_invalid_coords(self):
        marks = [{"text": "ok", "x": 0.5, "y": 0.3},
                 {"text": "", "x": 0.5, "y": 0.5},
                 {"text": "bad", "x": None, "y": 0.5},
                 "not a dict"]
        self.assertEqual(wm.normalize_marks(marks), [("ok", 0.5, 0.3)])

    def test_clamps_to_edges(self):
        # 超出 0~1 的坐标 clamp 到边缘，不丢水印（防御性，不因手滑丢内容）。
        marks = [{"text": "edge", "x": 5, "y": -1}]
        self.assertEqual(wm.normalize_marks(marks), [("edge", 1.0, 0.0)])

    def test_max_eight(self):
        marks = [{"text": str(i), "x": 0.1, "y": 0.1} for i in range(20)]
        self.assertEqual(len(wm.normalize_marks(marks)), 8)

    def test_safe_name(self):
        self.assertEqual(wm.safe_name("dy：233"), "dy233")
        self.assertEqual(wm.safe_name("ks：我是233"), "ks我是233")
        self.assertEqual(wm.safe_name(""), "wm")


class TaskPathTest(unittest.TestCase):
    def test_watermark_dir_and_path(self):
        t = comic.Task.__new__(comic.Task)
        t.dir = os.path.join("comics", "C20261121-001")
        self.assertEqual(t.watermark_dir,
                         os.path.join("comics", "C20261121-001", "watermark"))
        self.assertEqual(t.watermark_path("04", "dy233"),
                         os.path.join("comics", "C20261121-001",
                                      "watermark", "04_dy233.png"))


class RouteTest(unittest.TestCase):
    def test_route_exists_and_reads_only_final(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "app", "features", "comic", "routes.py"),
                   encoding="utf-8").read()
        self.assertIn('@bp.route("/api/comic/watermark", methods=["POST"])', src)
        self.assertIn("watermark_path(p.no, name)", src)
        self.assertIn("task.final_path(p)", src)
        # 水印 API 绝不写 raw/ final/
        seg = src[src.index("def api_comic_watermark"):]
        self.assertNotIn("task.raw_path(p)", seg)

    def test_studio_has_button_and_editor(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html = open(os.path.join(root, "web", "comic_studio.html"),
                    encoding="utf-8").read()
        self.assertIn("btnWatermark", html)
        self.assertIn("openWatermark()", html)
        self.assertIn("/api/comic/watermark", html)
        self.assertIn('id="wmCanvas"', html)


if __name__ == "__main__":
    unittest.main()
