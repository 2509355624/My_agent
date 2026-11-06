#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色库「只报名字 → 按渠道出图」（2026-11-06）。

用户拍板：每组底模只留一个代表渠道
    anima → silver / SDXL → miao / SD1.5 → image_gen_v1 / moss → moss
代码只把库里已有的 `channels` 透进候选块，选哪个交给大大怪。

验证三件事：
  1. `rep_channels` 四组映射对、跨架构按固定顺序排；
  2. `preview_block` 把渠道写进候选块，同名多架构合并成一行；
  3. `_MASTER_TEMPLATE` 有那条规则，且 `_translate` 能把模型填的渠道和角色
     一路带进生图结果（不落默认 silver）。
"""
import unittest
from unittest import mock

from app import direct_gen, lora_lib

_SDXL = {"key": "fushun", "name": "抚顺", "trigger": "fushun",
         "identity": "blue hair", "channels": ["miao", "cunny", "jank", "nffa"]}
_ANIMA = {"key": "hutao", "name": "胡桃", "trigger": "hutao",
          "identity": "brown hair", "channels": ["silver", "anima-line"]}


def _hit(key, name, score=0.9):
    return {"key": key, "name": name, "score": score}


class RepChannelsTest(unittest.TestCase):
    """四组架构 → 代表渠道。"""

    def test_anima_rep_is_silver(self):
        self.assertEqual(lora_lib.rep_channels(_ANIMA), ["silver"])

    def test_sdxl_rep_is_miao(self):
        self.assertEqual(lora_lib.rep_channels(_SDXL), ["miao"])

    def test_sd15_rep_is_image_gen_v1(self):
        self.assertEqual(
            lora_lib.rep_channels({"channels": ["nsfw_pose_gen"]}),
            ["image_gen_v1"])

    def test_moss_rep_is_moss(self):
        self.assertEqual(
            lora_lib.rep_channels({"channels": ["moss8", "moss_page"]}),
            ["moss"])

    def test_multi_arch_order_is_fixed(self):
        self.assertEqual(
            lora_lib.rep_channels({"channels": ["moss8", "miao", "silver"]}),
            ["silver", "miao", "moss"])

    def test_non_lora_channel_yields_nothing(self):
        self.assertEqual(lora_lib.rep_channels({"channels": ["krea2"]}), [])


class PreviewBlockTest(unittest.TestCase):
    """候选块把渠道这个事实带出来。"""

    def _render(self, entries, hits, text="抚顺"):
        with mock.patch.object(lora_lib, "entries", return_value=entries), \
             mock.patch.object(lora_lib, "search", return_value=hits):
            return lora_lib.preview_block(text)

    def test_sdxl_character_carries_miao(self):
        block = self._render([_SDXL], [_hit("fushun", "抚顺")])
        self.assertIn("- fushun｜抚顺｜渠道: miao", block)
        self.assertIn("trigger: fushun", block)

    def test_anima_character_carries_silver(self):
        block = self._render([_ANIMA], [_hit("hutao", "胡桃")], "胡桃")
        self.assertIn("渠道: silver", block)

    def test_same_key_multi_arch_merges_into_one_line(self):
        entries = [_ANIMA, dict(_ANIMA, channels=["miao", "nffa"])]
        hits = [_hit("hutao", "胡桃", 0.9), _hit("hutao", "胡桃", 0.8)]
        block = self._render(entries, hits, "胡桃")
        self.assertEqual(block.count("- hutao｜"), 1)
        self.assertIn("渠道: silver / miao", block)

    def test_below_gate_leaves_block_empty(self):
        self.assertEqual(
            self._render([_SDXL], [_hit("fushun", "抚顺", 0.3)]), "")

    def test_head_explains_the_rule(self):
        block = self._render([_SDXL], [_hit("fushun", "抚顺")])
        self.assertIn("没点名渠道", block)
        self.assertIn("第一个渠道", block)
        self.assertNotIn("Anima 角色库", block)


class TranslateTest(unittest.TestCase):
    """端到端：只报名字 → 渠道和角色都带进结果。"""

    def test_bare_name_keeps_the_rep_channel(self):
        seen = {}

        def fake_ask(content):
            seen["content"] = content
            return {"tool": "generate_image", "prompt": "1girl, blue hair",
                    "skill": "miao", "character": "fushun"}

        with mock.patch.object(lora_lib, "entries", return_value=[_SDXL]), \
             mock.patch.object(lora_lib, "search",
                               return_value=[_hit("fushun", "抚顺")]), \
             mock.patch.object(direct_gen, "_ask", side_effect=fake_ask):
            out = direct_gen._translate("抚顺", [])

        self.assertIsNotNone(out)
        self.assertEqual(out["skill"], "miao")
        self.assertEqual(out["character"], "fushun")
        self.assertIn("渠道: miao", seen["content"])
        self.assertIn("第一个渠道", seen["content"])

    def test_named_channel_still_wins(self):
        """用户点名了渠道：候选块不拼，照他说的走。"""
        def fake_ask(content):
            return {"tool": "generate_image", "prompt": "1girl",
                    "skill": "krea2", "character": ""}

        with mock.patch.object(direct_gen, "_ask", side_effect=fake_ask), \
             mock.patch.object(lora_lib, "preview_block",
                               return_value="SHOULD_NOT_APPEAR") as m_prev:
            out = direct_gen._translate("krea2 抚顺", [], skill="krea2")
        self.assertEqual(out["skill"], "krea2")
        m_prev.assert_not_called()

    def test_template_has_the_rule_and_drops_stale_wording(self):
        t = direct_gen._MASTER_TEMPLATE
        self.assertIn("第一个「渠道」", t)
        self.assertIn("没点渠道", t)
        self.assertNotIn("非 Anima 渠道", t)   # moss 已在角色库里，旧文案会误导


if __name__ == "__main__":
    unittest.main()
