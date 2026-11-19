# -*- coding: utf-8 -*-
"""完整写作示范块：真实回合、改前/改后、接入点。"""

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
    def test_example_is_long_enough_to_be_real_prose(self):
        """约 4,900 字的真实回合 + 改前/改后，不是角色卡开场白。"""
        self.assertGreaterEqual(len(writing_examples.EXAMPLE), 4500)
        self.assertLessEqual(len(writing_examples.EXAMPLE), 5500)

    def test_example_is_turn_based_not_first_message(self):
        text = writing_examples.EXAMPLE
        self.assertIn("## 真实回合示范", text)
        self.assertIn("玩家：", text)
        self.assertIn("角色：", text)
        self.assertIn("## 改前 / 改后", text)
        self.assertNotIn("first_mes", writing_examples.__doc__)
        self.assertIn("真实回合", writing_examples.__doc__)
        self.assertIn("examples.md", writing_examples.__doc__)

    def test_prompt_example_has_no_he_pronoun(self):
        """存档原文可以保留她的心声「他听见了」；送模型前必须纠成「你」。"""
        self.assertEqual(writing_examples.prompt_example().count("他"), 0)
        self.assertIn("你听见了", writing_examples.prompt_example())

    def test_block_has_header_and_body_once(self):
        block = writing_examples.full_example()
        self.assertTrue(block.startswith("## 完整示范"))
        self.assertIn(writing_examples.EXAMPLE, block)
        self.assertEqual(block.count("## 完整示范"), 1)

    def test_build_head_injects_block_once(self):
        head = persona.build_head(_card(), "private_1", "张三")
        self.assertIn("## 完整示范", head)
        self.assertEqual(head.count("## 完整示范"), 1)
        self.assertIn(writing_examples.prompt_example(), head)

    def test_group_head_does_not_take_the_private_example(self):
        head = persona.build_head(_card(), "group_123", "张三")
        self.assertNotIn("## 完整示范", head)


if __name__ == "__main__":
    unittest.main()
