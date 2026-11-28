# -*- coding: utf-8 -*-
"""从原文生成角色卡（2026-11-28）。纯逻辑，不碰真实卡库。"""

import json
import unittest
from unittest import mock

from app.features.persona import card_from_text as cft


class TruncateTest(unittest.TestCase):
    def test_short_text_untouched(self):
        text = "林晚低下头，声音很轻：「我不饿。」"
        body, cut = cft.truncate_text(text)
        self.assertEqual(body, text)
        self.assertFalse(cut)

    def test_long_text_clipped_with_mark(self):
        body, cut = cft.truncate_text("她" * 30_000)
        self.assertTrue(cut)
        self.assertLessEqual(len(body), cft.MAX_CHARS + 40)
        self.assertIn("【原文中段已省略", body)
        # 头和尾都要留着：开头有登场、结尾常有情绪高点
        self.assertTrue(body.startswith("她"))
        self.assertTrue(body.endswith("她"))

    def test_blank_text(self):
        body, cut = cft.truncate_text("")
        self.assertEqual(body, "")
        self.assertFalse(cut)


class PromptTest(unittest.TestCase):
    def test_prompt_carries_who_and_text(self):
        p = cft.build_prompt("原文第一段", "林晚")
        self.assertIn("林晚", p)
        self.assertIn("原文第一段", p)
        # 用户 2026-11-28 的口径：不许写写作指标条文
        self.assertNotIn("对白占七成", p)


class GenerateTest(unittest.TestCase):
    CARD = {
        "name": "林晚",
        "charName": "林晚",
        "description": "高二转学生",
        "personality": "说话软",
        "systemPrompt": "她是林晚",
        "exampleDialogue": "她：我不饿。\n你：吃点吧。",
        "opening": "你来了。",
        "room": "六叠房间",
        "appearancePrompt": "1girl, black hair",
        "outfitPrompt": "school uniform",
        # 这两类必须被剥掉 / 丢掉
        "start_value": 30,
        "char_lora": "should_drop",
    }

    def _ask(self, reply):
        return lambda messages: reply

    def test_fields_are_cleaned(self):
        card, err, meta = cft.generate(
            "原文", "林晚", ask=self._ask(json.dumps(self.CARD, ensure_ascii=False)))
        self.assertEqual(err, "")
        self.assertEqual(card["name"], "林晚")
        self.assertEqual(card["appearancePrompt"], "1girl, black hair")
        # 起点四条强制留空（用户拍板）
        self.assertIsNone(card.get("start_value"))
        # char_lora 不在玩家白名单
        self.assertFalse(card.get("char_lora"))

    def test_name_falls_back_to_who(self):
        card, err, _ = cft.generate(
            "原文", "林晚",
            ask=self._ask(json.dumps({"description": "x"}, ensure_ascii=False)))
        self.assertEqual(err, "")
        self.assertEqual(card["name"], "林晚")

    def test_empty_who_is_rejected(self):
        card, err, _ = cft.generate("原文", "  ")
        self.assertIsNone(card)
        self.assertIn("以谁为基准", err)

    def test_broken_json_retries_then_fails(self):
        calls = []

        def ask(messages):
            calls.append(1)
            return "抱歉我写不出来"

        card, err, _ = cft.generate("原文", "林晚", ask=ask)
        self.assertIsNone(card)
        self.assertEqual(len(calls), 2)          # 自动重问一次
        self.assertIn("没读懂", err)

    def test_bad_first_then_good(self):
        replies = iter(["不是JSON", json.dumps(self.CARD, ensure_ascii=False)])
        card, err, _ = cft.generate("原文", "林晚", ask=lambda m: next(replies))
        self.assertEqual(err, "")
        self.assertEqual(card["name"], "林晚")

    def test_json_fence_is_tolerated(self):
        fenced = "```json\n" + json.dumps(self.CARD, ensure_ascii=False) + "\n```"
        card, err, _ = cft.generate("原文", "林晚", ask=self._ask(fenced))
        self.assertEqual(err, "")
        self.assertEqual(card["name"], "林晚")


class SaveRenameTest(unittest.TestCase):
    def test_clash_gets_a_number(self):
        names = []

        def fake_make(qq, src):
            names.append(src["name"])
            if len(names) == 1:
                return None, "名字「林晚」和官方角色的卡撞了，换一个"
            return {"name": src["name"], "id": "u1_x"}, ""

        with mock.patch("app.features.persona.service.make_user_card", fake_make):
            card, err, renamed = cft.save("123", {"name": "林晚"})
        self.assertEqual(err, "")
        self.assertTrue(renamed)
        self.assertEqual(names, ["林晚", "林晚 2"])
        self.assertEqual(card["name"], "林晚 2")

    def test_full_shelf_does_not_retry(self):
        calls = []

        def fake_make(qq, src):
            calls.append(1)
            return None, "你自己的角色已经有 20 张了，先删一张再建"

        with mock.patch("app.features.persona.service.make_user_card", fake_make):
            card, err, renamed = cft.save("123", {"name": "林晚"})
        self.assertIsNone(card)
        self.assertEqual(len(calls), 1)          # 卡满加序号也解决不了，不重试
        self.assertIn("20 张", err)
        self.assertFalse(renamed)


class MarkdownTest(unittest.TestCase):
    def test_sections_and_dedup_title(self):
        md = cft.to_markdown({"name": "林晚", "charName": "林晚",
                              "description": "高二", "exampleDialogue": "她：嗯。"})
        self.assertTrue(md.startswith("# 林晚"))
        self.assertNotIn("（林晚）", md)          # 卡名和真名一样时不重复
        self.assertIn("## 她是谁", md)
        self.assertIn("她：嗯。", md)
        self.assertNotIn("## 性格", md)          # 空字段不出标题


if __name__ == "__main__":
    unittest.main()
