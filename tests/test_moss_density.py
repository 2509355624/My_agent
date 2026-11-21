# -*- coding: utf-8 -*-
"""MOSS 高密度小漫画：一格 2~4 句碎对话 + 气泡错落排版（2026-11-21）。

用户给的商业页漫例子：一页塞 15~20 句，一句就三五个字
（「咦？」「啊——」「是…」），一格装一个完整小回合。

原来管线把句数锁死在 2 句、autofix 也只排两个气泡位置——
第三句起就叠在一起。这里守三件事：
  1. 编剧规则放开到 2~4 句 / 3~8 字短句；
  2. 气泡位置有 4 条、互不重叠、从下往上按说话顺序；
  3. 同格有男主白边裸字时，气泡退到最下两条，和上半裸字不撞。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.comic import moss_twopass as m


CELL = [-246, -494, 244, -7]          # L3 中排左格


def _script(lines):
    return {
        "acts": [{"n": 1, "oneshot": "雨夜便利店", "lines": lines,
                  "scene_text": "A store entrance, a door, hard light.",
                  "narrs": []}],
        "cast": {"girl": "1girl, blonde"},
        "cast_text": "1girl, blonde",
        "arc": {},
    }


class WriterDensityRuleTest(unittest.TestCase):
    def test_script_rule_allows_two_to_four_lines(self):
        # 2026-11-21 两轮口径：①推翻「3~8 字碎句」，台词写完整；
        # ②随后用户指出旁白被撑成一大坨 → 改成「一句一框、每框一句短话」。
        self.assertIn("一句一个气泡", m.SCRIPT_SYSTEM)
        self.assertIn("十来个字是常态", m.SCRIPT_SYSTEM)
        self.assertNotIn("每句 3~8 字", m.SCRIPT_SYSTEM)
        self.assertIn("画面描述要写满，落进画面的文字框要短", m.SCRIPT_SYSTEM)

    def test_narration_is_one_short_line_per_box(self):
        # 旁白每框一句短话，不往一个白框堆长段（用户看商业参考图后的要求）
        self.assertIn("一个文字框只放一句短话", m.SCRIPT_SYSTEM)
        self.assertIn("绝不把好几句堆进同一个白框", m.SCRIPT_SYSTEM)
        self.assertNotIn("一格可以写两三句", m.SCRIPT_SYSTEM)

    def test_build_script_user_repeats_density(self):
        u = m.build_script_user("雨夜便利店，两人偶遇", 2)
        self.assertIn("十来个字是常态", u)
        self.assertNotIn("3~8 字", u)

    def test_contract_allows_four_bubbles(self):
        self.assertIn("一格最多四条气泡", m.MOSS_CONTRACT)
        self.assertNotIn("一格最多两条气泡", m.MOSS_CONTRACT)

    def test_bubble_caption_no_longer_forbids_neighbours(self):
        cap = m.bubble_cap("啊——")
        self.assertIn("several can share", cap)
        self.assertNotIn("single bubble only", cap)


class BubbleStackTest(unittest.TestCase):
    def test_four_bubble_boxes_do_not_overlap(self):
        boxes = [m.bbox_bubble(CELL, i) for i in range(4)]
        for i in range(len(boxes) - 1):
            # idx 0 最下、idx 3 最上；上一条底边（y 小）必须高于下一条顶边
            self.assertLess(boxes[i + 1][2], boxes[i][0],
                            "气泡 %d 与 %d 重叠" % (i, i + 1))
        for b in boxes:
            self.assertLess(b[0], b[2])
            self.assertLess(b[1], b[3])

    def test_bubble_order_is_bottom_up(self):
        boxes = [m.bbox_bubble(CELL, i) for i in range(4)]
        # 第 1 句最靠下，第 4 句最靠上
        self.assertEqual(boxes, sorted(boxes, key=lambda b: -b[0]))

    def test_plain_text_and_bubble_stay_in_separate_halves(self):
        plain = [m.bbox_plain(CELL, i) for i in range(2)]
        bubble = [m.bbox_bubble(CELL, i) for i in range(2)]
        self.assertLess(max(p[2] for p in plain),
                        min(b[0] for b in bubble))

    def test_moss_user_explains_stacking(self):
        s = _script([{"who": "girl", "text": "啊——", "cell": 2},
                     {"who": "girl", "text": "你在这", "cell": 2},
                     {"who": "girl", "text": "真巧", "cell": 2},
                     {"who": "girl", "text": "一起走", "cell": 2}])
        u = m.build_moss_user(s, 1, "", None, "L4")
        self.assertIn("从上到下的阅读顺序", u)
        self.assertIn("第 1 句最上、第 4 句最下", u)
        self.assertIn("共 4 句", u)


class AutofixDensityTest(unittest.TestCase):
    def _js(self):
        return {"items": [
            {"label": "character", "caption": "1girl", "bbox": [0, 0, 100, 100]},
        ]}

    def test_four_missing_girl_lines_all_get_stacked(self):
        js = self._js()
        lines = [{"who": "girl", "text": t, "cell": 2}
                 for t in ("啊——", "你在这", "真巧", "一起走")]
        notes = m.autofix(js, cast={"girl": "1girl, blonde"}, lines=lines,
                          layout="L4", scene="A store entrance.")
        texts = [it for it in js["items"] if it.get("label") == "text"]
        self.assertEqual(len(texts), 4)
        boxes = sorted((it["bbox"] for it in texts), key=lambda b: -b[0])
        for i in range(len(boxes) - 1):
            self.assertLess(boxes[i + 1][2], boxes[i][0])
        self.assertTrue(any("补漏台词" in n for n in notes))

    def test_boy_plain_keeps_bubbles_in_lower_half(self):
        js = self._js()
        lines = [{"who": "boy", "text": "走吧", "cell": 2},
                 {"who": "girl", "text": "啊——", "cell": 2},
                 {"who": "girl", "text": "等等", "cell": 2}]
        m.autofix(js, cast={"girl": "1girl, blonde"}, lines=lines,
                  layout="L4", scene="A store entrance.")
        plain = [it for it in js["items"] if it.get("label") == "text"
                 and m._kind(it.get("caption")) == "plain"]
        bubbles = [it for it in js["items"] if it.get("label") == "text"
                   and m._kind(it.get("caption")) == "bubble"]
        self.assertEqual(len(plain), 1)
        self.assertEqual(len(bubbles), 2)
        self.assertLess(max(p["bbox"][2] for p in plain),
                        min(b["bbox"][0] for b in bubbles))

    def test_parse_script_keeps_four_lines_in_one_cell(self):
        md = """# 雨夜
## 角色
- girl: 1girl, blonde
- boy: 1boy, dark hair
## 场景 1
A store entrance, a door, hard light.
## 页 1
场景：1
格1｜中景｜两人
画面：两人站在店门口。
台词：女「啊——」女「你在这」女「真巧」女「一起走」
状态：两人都在门外。
"""
        d = m.parse_script(md)
        self.assertEqual(len(d["acts"][0]["lines"]), 4)
        self.assertEqual(len(d["acts"][0]["cells"][1]["lines"]), 4)


if __name__ == "__main__":
    unittest.main()
