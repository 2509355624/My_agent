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


class ThreeTierFivePanelTest(unittest.TestCase):
    """五格「三层 1+2+2」（`1t2m2b`）。

    这个版式存在的**唯一理由**是格宽：四小格都是 512 宽，等于 2×2 的规整格，
    文字能按正常字数写；而 `3t2b` 的上排三格只有 341 宽。所以下面不但要钉
    几何，还要钉住「最窄格宽 ≥512」这个性质——一旦有人改成不规则铺法把
    某格挤窄，字数标准就跟不上了。
    """

    def boxes(self):
        return moss_comic.LAYOUT_BOXES["1t2m2b"]

    def test_five_panels(self):
        self.assertEqual(len(moss_comic.LAYOUTS["1t2m2b"][1]), 5)
        self.assertEqual(len(self.boxes()), 5)

    def test_covers_canvas_exactly(self):
        total = sum(_area(b) for b in self.boxes())
        self.assertEqual(total, moss_comic._CANVAS_W * moss_comic._CANVAS_H)

    def test_no_overlap(self):
        boxes = self.boxes()
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                self.assertFalse(_overlap(boxes[i], boxes[j]), (i, j))

    def test_reading_order_top_then_middle_then_bottom(self):
        """顺序必须和方位名对上：上 → 中左 → 中右 → 下左 → 下右。"""
        top, m1, m2, b1, b2 = self.boxes()
        # 三层的纵坐标严格递增
        self.assertLess(top[0], m1[0])
        self.assertLess(m1[0], b1[0])
        # 中排、下排各自左在右前
        self.assertLess(m1[1], m2[1])
        self.assertLess(b1[1], b2[1])
        # 上格横跨整幅
        self.assertEqual((top[1], top[3]),
                         (-moss_comic._HX, moss_comic._HX))

    def test_top_panel_is_the_biggest(self):
        """`key_panel` 契约要求「落在最大那格」，这里就是第 1 格。"""
        areas = [_area(b) for b in self.boxes()]
        self.assertEqual(areas.index(max(areas)), 0)
        self.assertEqual(max(areas), moss_comic._CANVAS_W * (moss_comic._CANVAS_H // 3))

    def test_every_panel_is_at_least_512_wide(self):
        """这是选它当默认五格的理由，不能被后来改成更窄的铺法。"""
        widths = [b[3] - b[1] for b in self.boxes()]
        self.assertGreaterEqual(min(widths), 512)
        self.assertEqual(min(widths), 512)

    def test_min_width_matches_the_letters_budget(self):
        """`layout_min_width` 要能算出真实的窄格，别处靠它提示字数。"""
        self.assertEqual(moss_comic.layout_min_width("1t2m2b"), 512)
        self.assertEqual(moss_comic.layout_min_width("2x2"), 512)
        self.assertEqual(moss_comic.layout_min_width("3x2"), 341)
        # 3t2b 的上排三格是 341，下排两格 512 → 取最窄
        self.assertEqual(moss_comic.layout_min_width("3t2b"), 341)
        self.assertEqual(moss_comic.layout_min_width("single"), 1024)
        self.assertEqual(moss_comic.layout_min_width("不存在的版式"), 0)


class TwoTierSixPanelTest(unittest.TestCase):
    """六格「上二下四」（`2t4b`，2026-10-31 起的默认六格）。

    存在的理由是**版式必须有主次**：六格等分（`3x2`）每格 341 宽、各占 16.7%，
    读起来像幻灯片，字数预算也只有 512 格的一半。这个版式把上排两格做成
    512×768（各 25%），下排四格 512×384（各 12.5%），面积比 2:1，格宽全 512。
    下面钉住这几条性质，防止后来被改成「看起来像上二下四、其实不均匀」。
    """

    def boxes(self):
        return moss_comic.LAYOUT_BOXES["2t4b"]

    def test_six_panels(self):
        self.assertEqual(len(moss_comic.LAYOUTS["2t4b"][1]), 6)
        self.assertEqual(len(self.boxes()), 6)

    def test_covers_canvas_exactly(self):
        total = sum(_area(b) for b in self.boxes())
        self.assertEqual(total, moss_comic._CANVAS_W * moss_comic._CANVAS_H)

    def test_no_overlap(self):
        boxes = self.boxes()
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                self.assertFalse(_overlap(boxes[i], boxes[j]), (i, j))

    def test_reading_order_top_row_then_bottom_grid(self):
        """顺序必须和方位名对上：上左 → 上右 → 中左 → 中右 → 下左 → 下右。"""
        top_l, top_r, mid_l, mid_r, bot_l, bot_r = self.boxes()
        # 上排整幅落在上半区，下排四格落在下半区
        for b in (top_l, top_r):
            self.assertEqual((b[0], b[2]), (-moss_comic._HY, 0))
        for b in (mid_l, mid_r, bot_l, bot_r):
            self.assertGreaterEqual(b[0], 0)
        self.assertEqual((mid_l[2], bot_l[2]), (moss_comic._QM, moss_comic._HY))
        # 同排左在右前
        self.assertLess(top_l[1], top_r[1])
        self.assertLess(mid_l[1], mid_r[1])
        self.assertLess(bot_l[1], bot_r[1])
        # 中排在排在前
        self.assertLess(mid_l[0], bot_l[0])

    def test_top_row_weighs_twice_the_bottom(self):
        """面积比 2:1 是「主次一眼可见」的全部来源，差 1.2 倍是看不出来的。"""
        areas = [_area(b) for b in self.boxes()]
        self.assertEqual(max(areas), 2 * min(areas))
        self.assertEqual(areas[:2], [max(areas)] * 2)

    def test_key_panel_constraint_is_satisfiable(self):
        """`key_panel` 要求「落在面积最大那格」——这里就是第 1 或第 2 格。"""
        areas = [_area(b) for b in self.boxes()]
        biggest = max(areas)
        self.assertEqual([i for i, a in enumerate(areas) if a == biggest], [0, 1])

    def test_every_panel_is_at_least_512_wide(self):
        """格宽全 512 = 2x2 的规整格，所以台词预算和 2x2 一致，这是选它当默认的理由。"""
        widths = [b[3] - b[1] for b in self.boxes()]
        self.assertEqual(set(widths), {512})
        self.assertEqual(moss_comic.layout_min_width("2t4b"), 512)


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


class MultiBubbleTest(unittest.TestCase):
    """一格两条气泡（2026-11-01 加）。

    背景：原来一格只有一个 `bubble` 字段、上限 10 字，一页最多 20 字文字，
    写不出「一问一答」。改成 `bubbles` 数组、单条仍 ≤11 字**靠条数堆量**。
    这里钉住的是「编译层真的落下来了」——`moss_api` 的 spec 清洗以前只保留
    四个键，`bubbles` 会在存盘那一步被静默丢掉（这台机器上踩过一次）。
    """

    @staticmethod
    def _spec(panels, layout="2t4b"):
        n = len(panels)
        shots = ["wide shot", "medium shot", "medium close-up",
                 "wide shot", "medium shot", "close-up"][:n]
        return {
            "title": "试",
            "style": "cool blue dusk light from the left",
            "cast": {"woman": "a young woman with short black hair, a grey blazer"},
            "pages": [{
                "n": 1, "layout": layout,
                "plan": {"key_panel": 1, "shots": shots},
                "scene": "a bus stop at dusk",
                "panels": panels,
            }],
        }

    def _desc(self, i):
        return ("A medium close-up of {woman} standing at the stop, warm dusk light "
                "on one side of her face, shot at eye level, panel %d." % i)

    def test_two_bubbles_become_two_text_items(self):
        spec = self._spec([{"desc": self._desc(1),
                            "bubbles": ["总算来了。", "……不是这班。"],
                            "caption": "", "sfx": ""}], layout="single")
        spec["pages"][0]["plan"]["shots"] = ["medium close-up"]
        d = json.loads(moss_comic.build_page_prompt(spec, spec["pages"][0]))
        texts = [i["text"] for i in d["items"]
                 if i["label"] == "text" and i["text"] in ("总算来了。", "……不是这班。")]
        self.assertEqual(len(texts), 2, d["items"])

    def test_two_bubbles_have_separate_boxes(self):
        """两个气泡的 bbox 不许重叠——贴在一起模型会画成一个大气泡。"""
        cell = (0, 0, 384, 512)
        a = moss_comic._text_bbox(cell, "bubble", 0)
        b = moss_comic._text_bbox(cell, "bubble", 1)
        self.assertFalse(_overlap(a, b), (a, b))

    def test_second_bubble_is_above_the_first(self):
        cell = (0, 0, 384, 512)
        a = moss_comic._text_bbox(cell, "bubble", 0)
        b = moss_comic._text_bbox(cell, "bubble", 1)
        self.assertLess(b[2], a[2])          # 上边更靠上
        self.assertLess(b[0], a[0])

    def test_legacy_bubble_field_still_works(self):
        """老 spec / 已存连载页全是 `bubble`，重画旧页必须还能读。"""
        got = moss_comic._bubbles({"bubble": "晚安", "bubbles": []})
        self.assertEqual(got, ["晚安"])

    def test_legacy_field_goes_first_when_both_present(self):
        got = moss_comic._bubbles({"bubble": "一", "bubbles": ["二"]})
        self.assertEqual(got, ["一", "二"])

    def test_only_the_cap_is_used(self):
        got = moss_comic._bubbles({"bubbles": ["一", "二", "三"]})
        self.assertEqual(len(got), moss_comic.MAX_BUBBLES)

    def test_blank_entries_are_dropped(self):
        got = moss_comic._bubbles({"bubbles": ["一", "", "  ", None]})
        self.assertEqual(got, ["一"])

    def test_check_reports_a_too_long_bubble(self):
        spec = self._spec([{"desc": self._desc(1),
                            "bubbles": ["这句话明显超过了十一个字的上限"],
                            "caption": "", "sfx": ""}], layout="single")
        spec["pages"][0]["plan"]["shots"] = ["medium close-up"]
        issues = moss_comic.check(spec)
        self.assertTrue(any("超 11 字" in s for s in issues), issues)

    def test_check_allows_a_long_caption_up_to_22(self):
        cap = "末班车晚了四十分钟，站牌上的时刻表还是去年的"   # 21 字
        spec = self._spec([{"desc": self._desc(1), "bubbles": [],
                            "caption": cap, "sfx": ""}], layout="single")
        spec["pages"][0]["plan"]["shots"] = ["medium close-up"]
        issues = moss_comic.check(spec)
        # single 只有一格，shots 必然只有一档，那条「整页会平」的提示是预期的
        self.assertFalse(any("超 22 字" in s or "长旁白" in s for s in issues), issues)

    def test_check_reports_two_long_captions_in_one_page(self):
        """长旁白只给最大的格用，一页两处就是叙述压过画面。"""
        cap = "末班车晚了四十分钟，站牌上的时刻表还是去年的"
        panels = [{"desc": self._desc(i), "bubbles": [], "caption": cap, "sfx": ""}
                  for i in (1, 2)]
        spec = self._spec(panels, layout="2h")
        spec["pages"][0]["plan"]["shots"] = ["wide shot", "close-up"]
        issues = moss_comic.check(spec)
        self.assertTrue(any("长旁白" in s for s in issues), issues)

    def test_caption_box_got_taller_for_the_long_tier(self):
        """长旁白靠双列竖排，框高要跟着放，不然 11 行压不进 18% 的格高。"""
        cell = (0, 0, 384, 512)
        box = moss_comic._text_bbox(cell, "caption")
        self.assertGreater(box[2] - box[0], 0.20 * 384)


class ExampleSpecTest(unittest.TestCase):
    """系统提示里那个示例，**自己必须先合自己的规**。

    模型对示例的模仿力度高于对规则的服从：示例里相邻两格写同一个景别，
    它整篇都会这么写；示例台词全空，它就一个字都不写（2026-11-01 换示例
    之前就是两格全空，等于在教模型「不要写台词」）。

    所以示例改完必须过一遍 `check()`，这条测试就是那道闸门。
    """

    def _example(self):
        sys_text = moss_comic._SYSTEM
        chunk = sys_text[sys_text.rindex("# 示例"):]
        spec, why = moss_comic.parse_json(
            chunk[chunk.find("{"):chunk.rfind("}") + 1])
        self.assertIsNotNone(spec, why)
        return spec

    def test_example_parses(self):
        self.assertTrue(self._example().get("pages"))

    def test_example_passes_its_own_check(self):
        issues = moss_comic.check(self._example())
        self.assertEqual(issues, [])

    def test_example_shows_two_bubbles(self):
        """示例要真的示范「一格两条」，光在规则里写不管用。"""
        got = [len(moss_comic._bubbles(p))
               for p in self._example()["pages"][0]["panels"]]
        self.assertIn(2, got, got)

    def test_example_compiles_to_layout_json(self):
        spec = self._example()
        d = json.loads(moss_comic.build_page_prompt(spec, spec["pages"][0]))
        texts = [i for i in d["items"] if i["label"] == "text"]
        self.assertEqual(len(texts), 5, [t["text"] for t in texts])


if __name__ == "__main__":
    unittest.main()
