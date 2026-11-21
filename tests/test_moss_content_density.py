"""2026-11-21：放开内容创作——长台词、多句旁白、格数跟叙事走。

用户明确：现在唯一要解的是内容。原来提示词里 3~8 字短句、一页 2~4 格、
旁白一格一句，把 AI 的创作压死了。这里只测「不再压制」和「解析接得住」，
出图归属/版式回归由别的测试文件管。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.comic import moss_twopass as m  # noqa: E402


SCRIPT = """# 熊脊
## 角色
- girl: 1girl, plain clothes
- boy: 1boy, hiking backpack
## 场景 1
A misty mountain trail, pine trees, a small hunter cabin, hard grey light through rain clouds.
## 页 1
场景：1
格1｜中景｜山脚
画面：清晨山脚，男人背着登山包站在山门下，抬头望着云雾里的山顶。
台词：男「我叫陆远，干了十二年探险。这座山，当地人叫它熊脊。」
旁白：「做过这么多次探险，我从来没有像今天这样紧张过。」
       「当地人反复叮嘱我，山里有熊；我笑着说我准备齐了。」
格2｜近景｜背包
画面：男人蹲下清点装备，绳索、打火石、急救包在背包口排开。
旁白：「绳索、打火石、急救包、一把柴刀——每一样我都检查了两遍。」
格3｜全景｜盘山道
画面：男人沿盘山道往上走，背影越来越小，雾从山谷里漫上来。
台词：男「今天，我要把这座山走到头。」
状态：他在山道上，背登山包，装备齐全；天在变阴。
"""


class TestPromptOpensContent(unittest.TestCase):
    def test_script_system_removes_restrictions(self):
        s = m.SCRIPT_SYSTEM
        self.assertIn("十来个字是常态", s)
        self.assertIn("叙事", s)
        self.assertNotIn("3~8 字", s)
        self.assertNotIn("碎句 3~8 字优先", s)
        self.assertNotIn("不写节拍表", s)
        self.assertNotIn("不写弧线", s)
        self.assertNotIn("不写「本页核心信息」", s)

    def test_script_system_allows_multi_narration(self):
        s = m.SCRIPT_SYSTEM
        # 2026-11-21 用户看商业参考图后收紧：旁白仍可多句交代，但**一句一框**，
        # 不再「一格两三句堆一个框」。
        self.assertIn("要交代两三件事就写成两三句、各占一格或各占一个框", s)
        self.assertIn("一个文字框只放一句短话", s)

    def test_script_system_defaults_to_single_panel(self):
        s = m.SCRIPT_SYSTEM
        # 用户 2026-11-21 定调：默认一页一格（多人同框），分镜只在必要时用；
        # 5/6 格从编剧层拿掉。
        self.assertIn("默认一页就是一格", s)
        self.assertIn("不要五格、六格", s)
        self.assertIn("不替他删格、合并格", s)
        self.assertNotIn("一页 1~6 格都行", s)
        # 三种合法拆格理由都在
        self.assertIn("三格", s)
        self.assertIn("四格", s)

    def test_script_user_defaults_to_single_panel(self):
        u = m.build_script_user("两个人在便利店门口聊天", 3)
        self.assertIn("一页默认就一格", u)
        self.assertIn("不要五格六格", u)

    def test_script_user_no_short_sentence_cap(self):
        u = m.build_script_user("一个悬疑故事", 3)
        self.assertIn("一句十来个字是常态", u)
        self.assertNotIn("3~8 字短句", u)
        self.assertNotIn("不要写弧线", u)

    def test_contract_keeps_long_lines(self):
        c = m.MOSS_CONTRACT
        self.assertIn("不许为凑短而删字或改写成碎句", c)
        self.assertNotIn("碎句 3~8 字优先", c)


class TestParseRichContent(unittest.TestCase):
    def setUp(self):
        self.s = m.parse_script(SCRIPT)
        self.a = self.s["acts"][0]

    def test_long_line_kept_verbatim(self):
        lines = [x["text"] for x in self.a["lines"]]
        self.assertIn("我叫陆远，干了十二年探险。这座山，当地人叫它熊脊。", lines)
        self.assertTrue(any(len(x) > 15 for x in lines))

    def test_multi_narration_all_kept(self):
        texts = [e["text"] for e in self.a["narrs"]]
        self.assertEqual(len(texts), 3)
        self.assertIn("当地人反复叮嘱我，山里有熊；我笑着说我准备齐了。", texts)

    def test_narrs_carry_cell(self):
        by_cell = {}
        for e in self.a["narrs"]:
            by_cell.setdefault(e["cell"], []).append(e["text"])
        self.assertEqual(len(by_cell[1]), 2)
        self.assertEqual(len(by_cell[2]), 1)

    def test_grid_from_actual_cells(self):
        self.assertEqual(self.a["grid"], 3)
        # 2026-11-21：三格默认从 L5（左通高 1:3.1，人会被拉长）换成 T3。
        self.assertEqual(m._layout_for(self.a["grid"]), "T3")


class TestTextColumns(unittest.TestCase):
    def test_columns_scale_with_length(self):
        self.assertEqual(m._text_columns("短"), 1)
        self.assertEqual(m._text_columns("十来个字的一句话啊啊啊"), 2)
        self.assertEqual(m._text_columns("一" * 25), 3)

    def test_bubble_cap_uses_columns(self):
        cap = m.bubble_cap("十来个字的一句话啊啊啊")
        self.assertIn("2 columns", cap)

    def test_boy_text_cap_uses_columns(self):
        cap = m.boy_text_cap("一" * 25)
        self.assertIn("3 columns", cap)

    def test_multi_caption_boxes_no_overlap(self):
        cell = list(m.SKELETONS["L4"][0])
        b0 = m.bbox_caption(cell, 0)
        b1 = m.bbox_caption(cell, 1)
        self.assertLess(b0[2], b1[0])  # 第一框底 < 第二框顶


if __name__ == "__main__":
    unittest.main()
