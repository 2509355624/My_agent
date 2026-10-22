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

    def test_fixed_block_is_identity_only_and_each_panel_writes_clothes_once(self):
        fixed = ("1girl, 1boy, Illyasviel, faceless man, silver-white long hair, "
                 "red eyes, purple hair ribbon, twin tails, black short hair, grey eyes")
        panel = ("convenience store at night, girl wearing lavender sailor collar blouse "
                 "and dark purple pleated skirt, boy wearing dark navy zip-up hoodie "
                 "and grey cargo pants, girl reaching for the door, boy standing beside her, "
                 "girl neutral expression, boy slight smile, front view, medium shot, "
                 "cool fluorescent light")
        raw = ["# 标题：便利店", "固定部分: " + fixed]
        for i in range(1, 3):
            raw += ["", "## %02d" % i, "画面: " + panel]
        text = silver_comic.parse("\n".join(raw), 2)
        self.assertNotIn("wearing", fixed)
        # 固定部分拼到每格开头，服饰只在每格动态画面里出现一次，不重复两遍
        self.assertEqual(text.count("girl wearing lavender sailor collar blouse"), 2)
        self.assertEqual(text.count("boy wearing dark navy zip-up hoodie"), 2)
        self.assertEqual(text.count("silver-white long hair"), 2)
        for line in text.splitlines():
            if line.startswith("画面:"):
                self.assertTrue(line.startswith("画面: " + fixed))

    def test_write_script_prompt_keeps_clothes_out_of_fixed_block_and_calls_llm_once(self):
        with mock.patch.object(silver_comic.llm, "call_llm",
                               return_value=self._raw(panels=1)) as call:
            text = silver_comic.write_script("两个人在车站等雨停", panels=1)
        system = call.call_args.args[0][0]["content"]
        self.assertEqual(call.call_count, 1)
        self.assertIn("固定部分不写服饰", system)
        self.assertIn("每格必须把每个入画角色的服饰完整写一遍", system)
        self.assertIn("每格只写一遍服饰", system)
        self.assertIn("除非剧情明确换装", system)
        self.assertIn("## 01", text)


if __name__ == "__main__":
    unittest.main()
