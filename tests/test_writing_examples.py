# -*- coding: utf-8 -*-
"""完整写作示范块：静态前缀、长度、去 AI 痕迹与接入点。"""

import re
import unittest

from app.features.persona import service as persona
from app.features.persona import writing_examples


def _card():
    return {
        "id": "t", "name": "测试角色", "description": "《某作品》里的人",
        "avatar": "", "personality": "羞涩、认真",
        "systemPrompt": "你是测试角色，说话很轻。",
        "exampleDialogue": "", "appearancePrompt": "", "outfitPrompt": "",
        "room": "", "referenceImage": "", "stateBehavior": {},
    }


class WritingExampleTest(unittest.TestCase):
    def test_example_is_700_to_800_chars(self):
        self.assertGreaterEqual(len(writing_examples.EXAMPLE), 700)
        self.assertLessEqual(len(writing_examples.EXAMPLE), 800)

    def test_example_avoids_mechanical_ticks(self):
        text = writing_examples.EXAMPLE
        # 「像潮水 / 像钉子」是正常白描；这里禁的是 AI 腔的解释式比喻
        # 「像是……」「好像……」。
        for bad in ("顿了顿", "停了", "卡了", "像是", "好像"):
            self.assertNotIn(bad, text)
        # 不要用「不是……是……」解释情绪。
        self.assertIsNone(re.search(r"不是[^。！？\n]{0,14}是", text))
        # 范文必须来自真实角色卡，不是自己写的；保留来源注释。
        self.assertIn("Chub", writing_examples.__doc__)
        self.assertIn("old_charity_66451", writing_examples.__doc__)
        # 系统头里不能出现「他」：模型会把范文里的代词当示范学走。
        self.assertEqual(text.count("他"), 0)
        self.assertIn("她", text)

    def test_block_has_header_and_body_once(self):
        block = writing_examples.full_example()
        self.assertTrue(block.startswith("## 完整示范"))
        self.assertIn(writing_examples.EXAMPLE, block)
        self.assertEqual(block.count("## 完整示范"), 1)

    def test_build_head_injects_block_once(self):
        head = persona.build_head(_card(), "private_1", "张三")
        self.assertIn("## 完整示范", head)
        self.assertEqual(head.count("## 完整示范"), 1)
        self.assertIn(writing_examples.EXAMPLE, head)

    def test_group_head_does_not_take_the_private_example(self):
        head = persona.build_head(_card(), "group_123", "张三")
        self.assertNotIn("## 完整示范", head)


if __name__ == "__main__":
    unittest.main()
