# -*- coding: utf-8 -*-
"""本轮事实点清单（2026-11-08）：第一段列事实，演员照单落地。

钉住三件事：清单只裁剪不判断；三份打分模板都要交 facts；
run_turn 把清单放在场景义务之后、尾注之前，玩家原文仍然完整。

背景：玩家一段话里并列的道具（油菜 / 西红柿 / 豆干 / 鸡蛋 / 方便面）
被第一段「做→一句话概括」压成「他拿出食材」，演员只接住最后一句台词，
甚至反问「要不要加个鸡蛋」。清单是事实，不是文风；落地交给模型。
"""

import os
import re
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.affection as affection
import app.persona as persona


KITCHEN_FACTS = [
    "一把小油菜", "几个西红柿", "豆干放在案板上",
    "中午和晚上的菜分好放进冰箱", "几颗鸡蛋", "两块方便面",
    "早餐煮西红柿鸡蛋汤面加豆干",
]


class FactsParseTest(unittest.TestCase):
    def test_crops_and_dedups_without_inventing(self):
        out = persona._parse_facts([
            "  一把  小油菜 ", "一把 小油菜", {"text": "几颗鸡蛋"},
            "", None, 123,
        ])
        self.assertEqual(out, ["一把 小油菜", "几颗鸡蛋", "123"])

    def test_limits(self):
        out = persona._parse_facts(["x%d" % i for i in range(30)])
        self.assertEqual(len(out), persona.FACT_MAX)
        self.assertTrue(all(len(s) <= persona.FACT_TEXT_MAX for s in out))

    def test_no_facts_is_noop(self):
        self.assertEqual(persona._parse_facts("一把小油菜"), [])
        self.assertEqual(persona._facts_block(None), "")
        self.assertEqual(persona._facts_block([]), "")


class FactsBlockTest(unittest.TestCase):
    def test_keeps_written_order(self):
        block = persona._facts_block(KITCHEN_FACTS)
        pos = [block.index(f) for f in KITCHEN_FACTS]
        self.assertEqual(pos, sorted(pos))
        for f in KITCHEN_FACTS:
            self.assertIn("- " + f, block)

    def test_says_these_are_already_in_this_scene(self):
        # 契约里「话里交代的前因，她没跟着去过」和场景义务打架，
        # 清单必须正面说清这些就在眼前，而不是去改契约正文。
        block = persona._facts_block(KITCHEN_FACTS)
        self.assertIn("就在这一幕里", block)
        self.assertIn("各落地一次", block)
        self.assertIn("由她的动作带过去", block)

    def test_positive_wording_only(self):
        # 白象效应：堆「不要复述」反而会把复述点亮一遍。
        block = persona._facts_block(KITCHEN_FACTS)
        for banned in ("不要", "不许", "禁止", "别把"):
            self.assertNotIn(banned, block)


class FactsPromptTest(unittest.TestCase):
    def test_all_three_prompts_ask_for_facts(self):
        for name in ("_INTENT_PROMPT", "_SCORE_PROMPT", "_SCORE_PROMPT_V2"):
            t = getattr(persona, name)
            self.assertIn('"facts"', t, name)
            keys = set(re.findall(r"%\(([^)]+)\)", t))
            self.assertTrue(any("facts_guide" in k for k in keys), name)
            # 每个占位符都能被填掉（`%(...)d` 这类数值占位也要算进去）。
            filled = t % {k: 0 for k in keys}
            self.assertIn("这一幕已经有的东西", filled, name)

    def test_director_fields_carry_facts(self):
        info = persona._director_fields({"facts": KITCHEN_FACTS})
        self.assertEqual(info["facts"], KITCHEN_FACTS)
        self.assertEqual(persona._director_fields(None)["facts"], [])


class FactsInjectTest(unittest.TestCase):
    """端到端：第一段交的 facts 真的进演员最后一条消息。"""

    KEY = "private_facts_1"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", root)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(
            persona, "_STATE_PATH",
            os.path.join(self.tmp.name, "state", "persona_sessions.json"))
        q.start()
        self.addCleanup(q.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        v = mock.patch.object(affection, "V2_AUTO_NEW", False)
        v.start()
        self.addCleanup(v.stop)
        card = {"id": "", "name": "学妹", "description": "", "avatar": "",
                "personality": "", "systemPrompt": "你是学妹。",
                "appearancePrompt": "", "outfitPrompt": "",
                "referenceImage": ""}
        persona.save_cards([card])
        persona.switch_line(self.KEY, persona.find_card("学妹"))

    def test_facts_after_scene_duty_before_tail(self):
        score = {"segments": [], "addressed": True, "facts": KITCHEN_FACTS}
        raw = ("她把菜叶捞起来。\n画面：1girl\n心情：平静 强度1\n"
               "状态：厨房 · 围裙 · 无 · 早晨")
        with mock.patch.object(persona, "_judge_intent_scored",
                               return_value=("neutral", "", score)), \
             mock.patch.object(persona.llm, "call_llm",
                               return_value=raw) as m:
            persona.run_turn(self.KEY, "我拿出了几颗鸡蛋", "private", "")
        msgs = m.call_args_list[-1][0][0]
        user = msgs[-1]["content"]
        self.assertIn("我拿出了几颗鸡蛋", user)          # 原文没被摘要顶掉
        self.assertIn("【这一幕已经有的东西（按他写的顺序）】", user)
        self.assertIn("两块方便面", user)
        self.assertLess(user.index("他做的动作，用叙述演出来"),
                        user.index("【这一幕已经有的东西"))
        # 尾注仍是模型读到的最后一句（带卡名时开头是「（正文用…叫她」）。
        self.assertIn("（正文用", user)
        self.assertLess(user.index("【这一幕已经有的东西"),
                        user.index("（正文用"))

    def test_no_facts_keeps_old_shape(self):
        """编剧没交 facts（或调用失败）时整块不出，行为跟改动前一样。"""
        score = {"segments": [], "addressed": True}
        raw = "她应了一声。\n画面：1girl\n心情：平静 强度1\n状态：厨房"
        with mock.patch.object(persona, "_judge_intent_scored",
                               return_value=("neutral", "", score)), \
             mock.patch.object(persona.llm, "call_llm",
                               return_value=raw) as m:
            persona.run_turn(self.KEY, "在吗", "private", "")
        user = m.call_args_list[-1][0][0][-1]["content"]
        self.assertNotIn("【这一幕已经有的东西", user)
        self.assertIn("他做的动作，用叙述演出来", user)


if __name__ == "__main__":
    unittest.main()
