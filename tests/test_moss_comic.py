"""`app/moss_comic.py` 的版式几何与提示词编译测试（不调模型、不出图）。

钉住三件事：
1. 每个版式的 bbox 数量与方位名数量一致（`build_page_prompt` 按下标取格子，
   数量对不上就会把最后一格重复用）；
2. 3×2 的六个格子必须**精确铺满画布且互不重叠**——差一个像素就会在格间露出
   白缝，重叠则会把相邻格的内容画穿；
3. 六格 spec 能编译成合法的 Layout JSON，且分格句里写明六格。
"""

import json
import unittest

from app import moss_comic


def _area(box):
    y0, x0, y1, x1 = box
    return (y1 - y0) * (x1 - x0)


def _overlap(a, b):
    """两个 yxyx 矩形是否真的相交（共边不算）。"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class LayoutGeometryTest(unittest.TestCase):
    def test_every_layout_has_matching_boxes(self):
        for name, lay in moss_comic.LAYOUTS.items():
            names = lay[1]
            boxes = moss_comic.LAYOUT_BOXES.get(name)
            self.assertIsNotNone(boxes, name)
            self.assertEqual(len(boxes), len(names), name)

    def test_three_by_two_covers_canvas(self):
        boxes = moss_comic.LAYOUT_BOXES["3x2"]
        self.assertEqual(len(boxes), 6)
        total = sum(_area(b) for b in boxes)
        self.assertEqual(total, moss_comic._CANVAS_W * moss_comic._CANVAS_H)

    def test_three_by_two_has_no_overlap(self):
        boxes = moss_comic.LAYOUT_BOXES["3x2"]
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                self.assertFalse(_overlap(boxes[i], boxes[j]), (i, j))

    def test_three_by_two_is_left_to_right_then_down(self):
        """顺序必须和方位名一一对应，否则第一格描述会画到右下角去。

        左中右 → 上排三个 x 依次递增；下排三个整体在下半区。
        """
        boxes = moss_comic.LAYOUT_BOXES["3x2"]
        top, bottom = boxes[:3], boxes[3:]
        for row in (top, bottom):
            self.assertLess(row[0][1], row[1][1])
            self.assertLess(row[1][1], row[2][1])
        for b in top:
            self.assertEqual((b[0], b[2]), (-moss_comic._HY, 0))
        for b in bottom:
            self.assertEqual((b[0], b[2]), (0, moss_comic._HY))

    def test_three_by_two_columns_are_even(self):
        """三列宽度最多差 1 像素（1024 除不尽 3），差多了格线会明显歪。"""
        boxes = moss_comic.LAYOUT_BOXES["3x2"]
        widths = [b[3] - b[1] for b in boxes[:3]]
        self.assertLessEqual(max(widths) - min(widths), 1)

    def test_all_bboxes_inside_canvas(self):
        hx, hy = moss_comic._CANVAS_W // 2, moss_comic._CANVAS_H // 2
        for name, boxes in moss_comic.LAYOUT_BOXES.items():
            for b in boxes:
                self.assertGreaterEqual(b[0], -hy, name)
                self.assertGreaterEqual(b[1], -hx, name)
                self.assertLessEqual(b[2], hy, name)
                self.assertLessEqual(b[3], hx, name)


class SixPanelPromptTest(unittest.TestCase):
    @staticmethod
    def _spec(n=6, layout="3x2"):
        # 2026-10-31 起 `check()` 会校验 plan（整页视觉规划）：shots 长度要
        # 等于格数、相邻不许同景别、整页至少三档。测试 spec 必须带上，
        # 否则 `test_check_passes_full_six` 会多报一条「没有 plan」。
        shots = ["wide shot", "medium shot", "medium close-up",
                 "wide shot", "medium shot", "close-up"][:n]
        return {
            "title": "开门",
            "style": "warm indoor window light from the upper left",
            "cast": {"woman": "a young woman with pale lilac hair, a white robe"},
            "pages": [{
                "n": 1, "layout": layout,
                "plan": {"key_panel": 1, "shots": shots},
                "scene": "a small apartment doorway in the morning",
                "panels": [{"desc": "Wide shot of {woman} standing in the doorway holding "
                                    "a grocery bag, eye level, warm light from the corridor "
                                    "behind her.",
                            "bubble": "我回来了", "caption": "清晨", "sfx": ""}
                           for _ in range(n)],
            }],
        }

    def test_prompt_is_valid_json(self):
        spec = self._spec()
        out = moss_comic.build_page_prompt(spec, spec["pages"][0])
        d = json.loads(out)
        self.assertEqual(d["bbox_format"], "yxyx")
        self.assertEqual(d["bbox_coord_space"], "abs_centralized")
        self.assertEqual(d["summary"]["canvas"], "multi_panel")

    def test_six_character_items_use_six_boxes(self):
        spec = self._spec()
        d = json.loads(moss_comic.build_page_prompt(spec, spec["pages"][0]))
        chars = [i for i in d["items"] if i["label"] == "character"]
        self.assertEqual(len(chars), 6)
        self.assertEqual([c["bbox"] for c in chars],
                         moss_comic.LAYOUT_BOXES["3x2"])

    def test_split_sentence_says_six_panels(self):
        spec = self._spec()
        d = json.loads(moss_comic.build_page_prompt(spec, spec["pages"][0]))
        caption = d["summary"]["caption"]
        self.assertIn("exactly six panels", caption)
        self.assertIn("two by three grid", caption)

    def test_check_reports_wrong_panel_count(self):
        spec = self._spec(n=5)
        issues = moss_comic.check(spec)
        self.assertTrue(any("要 6 格" in s for s in issues), issues)

    def test_check_passes_full_six(self):
        self.assertEqual(moss_comic.check(self._spec()), [])

    def test_two_by_two_still_works(self):
        """老版式不能被这次的改动带坏。"""
        spec = self._spec(n=4, layout="2x2")
        d = json.loads(moss_comic.build_page_prompt(spec, spec["pages"][0]))
        chars = [i for i in d["items"] if i["label"] == "character"]
        self.assertEqual(len(chars), 4)
        self.assertEqual([c["bbox"] for c in chars],
                         moss_comic.LAYOUT_BOXES["2x2"])


if __name__ == "__main__":
    unittest.main()
