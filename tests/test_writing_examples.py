# -*- coding: utf-8 -*-
"""范文段已停用：写法全部由 writing_guide.py 告诉 AI 怎么做。

旧的「本机存档真实回合 + 改前改后」是已驳回的写法，不再注入系统头。
这里守两件事：范文接口保持空、系统头不再出现范文块。
"""

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
    def test_example_interface_is_empty(self):
        """范文停用后两个入口都返回空串，调用方 import 不用改。"""
        self.assertEqual(writing_examples.EXAMPLE if hasattr(
            writing_examples, "EXAMPLE") else "", "")
        self.assertEqual(writing_examples.full_example(), "")
        self.assertEqual(writing_examples.prompt_example(), "")

    def test_no_bad_example_sources_in_module(self):
        self.assertNotIn("改前 / 改后", writing_examples.__doc__)
        self.assertNotIn("真实回合", writing_examples.__doc__)

    def test_build_head_has_no_example_block(self):
        for key in ("private_1", "group_123"):
            head = persona.build_head(_card(), key, "张三")
            self.assertNotIn("## 完整示范", head)
            self.assertNotIn("## 真实回合示范", head)

    def test_build_head_keeps_the_writing_guide(self):
        """范文走了，规则还在：写法说明仍进每一轮。"""
        head = persona.build_head(_card(), "private_1", "张三")
        self.assertIn("## 写这一轮", head)
        self.assertIn("从上一轮接着写", head)
        self.assertIn("玩家只说一句话", head)
        self.assertIn("一段连着的剧情", head)


if __name__ == "__main__":
    unittest.main()
