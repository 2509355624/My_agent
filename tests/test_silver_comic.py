# -*- coding: utf-8 -*-
"""Silver 自由视角漫画：严格解析和一次 API 契约。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import silver_comic


class SilverParseTest(unittest.TestCase):
    def _raw(self, panels=2, bubbles=False):
        out = ["# 标题：公交站", "固定部分: girl with black bob hair and brown eyes, boy with short black hair and gray eyes"]
        for i in range(1, panels + 1):
            out += ["", "## %02d" % i,
                    "画面: inside a bus stop, girl and boy standing together, medium shot"]
            if bubbles:
                out += ["气泡: （女）雨停了吗？", "气泡2: （男）快了"]
        return "\n".join(out)

    def test_parse_no_bubble(self):
        text = silver_comic.parse(self._raw(), 2, mode="no_bubble")
        self.assertIn("## 01", text)
        self.assertNotIn("气泡:", text)
        self.assertEqual(text.count("画面:"), 2)

    def test_parse_bubble_keeps_speakers(self):
        text = silver_comic.parse(self._raw(bubbles=True), 2, mode="bubble")
        self.assertEqual(text.count("气泡:"), 2)
        self.assertEqual(text.count("气泡2:"), 2)
        self.assertIn("（女）雨停了吗？", text)
        self.assertIn("（男）快了", text)

    def test_fixed_block_aliases_are_accepted(self):
        for label in ("固定块:", "固定角色：", "角色固定:"):
            with self.subTest(label=label):
                raw = self._raw().replace("固定部分:", label)
                text = silver_comic.parse(raw, 2)
                self.assertEqual(text.count("画面:"), 2)

    def test_fixed_label_on_its_own_line_is_accepted(self):
        raw = self._raw().replace(
            "固定部分: girl with black bob hair and brown eyes, boy with short black hair and gray eyes",
            "固定部分:\ngirl with black bob hair and brown eyes, boy with short black hair and gray eyes")
        self.assertEqual(silver_comic.parse(raw, 2).count("画面:"), 2)

    def test_missing_fixed_rejected(self):
        with self.assertRaisesRegex(ValueError, "缺固定部分"):
            silver_comic.parse("## 01\n画面: inside a room", 1)

    def test_panel_count_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "要求 2 格"):
            silver_comic.parse(self._raw(panels=1), 2)

    def test_chinese_scene_rejected(self):
        with self.assertRaisesRegex(ValueError, "画面含中文"):
            silver_comic.parse(self._raw().replace("inside a bus stop", "公交站"), 2)

    def test_fixed_block_and_each_panel_keep_complete_two_person_clothes(self):
        fixed = (
            "1girl, Illyasviel, silver-white long hair, red eyes, purple ribbon, "
            "girl wearing purple wool coat, black tights, brown ankle boots, "
            "1boy, faceless man, short black hair, gray eyes, "
            "boy wearing dark navy jacket, gray trousers, black sneakers"
        )
        raw = ["# 标题：便利店", "固定部分: " + fixed]
        for i in range(1, 3):
            raw += [
                "", "## %02d" % i,
                "画面: convenience store at night, girl wearing purple wool coat, "
                "black tights and brown ankle boots, boy wearing dark navy jacket, "
                "gray trousers and black sneakers, both standing by the shelves, "
                "medium shot, cool fluorescent light",
            ]
        text = silver_comic.parse("\n".join(raw), 2)
        self.assertEqual(text.count("girl wearing purple wool coat"), 4)
        self.assertEqual(text.count("boy wearing dark navy jacket"), 4)
        self.assertEqual(text.count("brown ankle boots"), 4)
        self.assertEqual(text.count("black sneakers"), 4)

    def test_write_script_prompt_requires_stable_clothes_and_calls_llm_once(self):
        with mock.patch.object(silver_comic.llm, "call_llm",
                               return_value=self._raw(panels=1)) as call:
            text = silver_comic.write_script("两个人在车站等雨停", panels=1)
        self.assertEqual(call.call_count, 1)
        self.assertIn("完整服饰", call.call_args.args[0][0]["content"])
        self.assertIn("再次按角色写出完整服饰", call.call_args.args[0][0]["content"])
        self.assertIn("除非剧情明确发生换装", call.call_args.args[0][0]["content"])
        self.assertIn("## 01", text)


if __name__ == "__main__":
    unittest.main()
