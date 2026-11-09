# -*- coding: utf-8 -*-
"""服饰状态闭环（2026-11-09）：解析 → 回注 → 固化 → 出图前置。

用户实测的问题：「上一个场景明明穿着外套，下一个场景脱掉了，第三个场景
又变成穿着外套」。盘上查到的两处根因：

1. 服饰只写在每轮自由输出的画面行里，没有独立状态、没有专门回注、没有
   固化；演员只能从滑动窗口里混在正文后的三行元数据去猜。
2. 角色卡的 `outfitPrompt` 每轮硬拼在画面行前面，那件写死的外套永远都在，
   和「脱掉了」直接打架。

这一组钉住闭环的四段，并守住「不改 `split_turn` 五元组签名」这条底线
（十几个生产调用点 + 老单测都按它拆包）。
"""

import os
import tempfile
import unittest
from unittest import mock

import app.affection as affection
import app.persona as persona

RAW = (
    "她把外套搭到椅背上。\n\n"
    "画面：medium shot, smiling, warm light\n"
    "服饰：white shirt with sleeves rolled up, black pleated skirt\n"
    "心情：害羞 强度6\n"
    "状态：卧室 · 白衬衫 · 站着 · 傍晚 · 无\n"
)
OUTFIT = "white shirt with sleeves rolled up, black pleated skirt"
PREV = "loose beige knit sweater and grey lounge shorts"


class OutfitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(
            persona, "_STATE_PATH",
            os.path.join(self.tmp.name, "state", "persona_sessions.json"))
        p.start()
        self.addCleanup(p.stop)
        v = mock.patch.object(affection, "V2_AUTO_NEW", False)
        v.start()
        self.addCleanup(v.stop)


class SplitOutfitTest(OutfitBase):
    def test_extracts_last_line_and_strips_noise(self):
        self.assertEqual(persona.split_outfit(RAW), OUTFIT)
        self.assertEqual(persona.split_outfit("画面：x\n服饰:**pink pajama top**"),
                         "pink pajama top")
        self.assertEqual(persona.split_outfit("画面：x\n服饰:外套\n服饰:睡衣"),
                         "睡衣")
        self.assertEqual(persona.split_outfit("就一句话，没有元数据"), "")
        self.assertEqual(persona.split_outfit("服饰：   "), "")

    def test_split_turn_strips_it_and_keeps_five_tuple(self):
        got = persona.split_turn(RAW)
        self.assertEqual(len(got), 5)
        body, scene, mood, intensity, state = got
        self.assertEqual(body, "她把外套搭到椅背上。")
        self.assertNotIn("服饰", body)
        self.assertNotIn(OUTFIT, body)
        self.assertEqual(scene, "medium shot, smiling, warm light")
        self.assertEqual(mood, "害羞")
        self.assertEqual(intensity, 6)
        self.assertEqual(state, "卧室 · 白衬衫 · 站着 · 傍晚 · 无")

    def test_legacy_three_line_output_is_unchanged(self):
        old = "她笑了。\n\n画面：close-up\n心情：开心 强度3\n状态：家里 · 睡衣 · 坐着 · 傍晚 · 无\n"
        body, scene, mood, intensity, state = persona.split_turn(old)
        self.assertEqual(body, "她笑了。")
        self.assertEqual(scene, "close-up")
        self.assertEqual(mood, "开心")
        self.assertEqual(intensity, 3)
        self.assertTrue(state.startswith("家里"))


class EnsureMetaTest(OutfitBase):
    def test_keeps_the_line_the_model_wrote(self):
        self.assertIn("服饰：" + OUTFIT,
                      persona._ensure_meta(RAW, "", "害羞", 6, "", OUTFIT))

    def test_fills_missing_line_from_previous_outfit(self):
        raw = ("她笑了。\n\n画面：close-up\n心情：开心 强度3\n"
               "状态：家里 · 睡衣 · 坐着 · 傍晚 · 无\n")
        out = persona._ensure_meta(raw, "", "开心", 3, "", PREV)
        self.assertIn("服饰：" + PREV, out)
        # 补进去的那行不会漏进正文（历史里四行齐全）
        self.assertNotIn("服饰", persona.split_turn(out)[0])

    def test_no_outfit_argument_never_invents_a_line(self):
        """老调用点不传 outfit：只保护原文里已有的行，不凭空造一行。"""
        raw = "她笑了。\n\n画面：close-up\n心情：开心 强度3\n状态：家里\n"
        self.assertNotIn("服饰：", persona._ensure_meta(raw, "", "开心", 3, ""))


class StateTest(OutfitBase):
    KEY = "web_outfit_test__card"

    def _seed(self, **kw):
        ent = {"card": "card", "note": "", "turns": 0, "mood": "",
               "intensity": 0, "state": ""}
        ent.update(kw)
        persona._save_state({self.KEY: ent})

    def test_save_mood_persists_outfit_and_snapshots_it(self):
        self._seed(outfit=PREV)
        persona._save_mood(self.KEY, "害羞", 6, "卧室 · 白衬衫 · 站着 · 傍晚 · 无",
                           outfit=OUTFIT)
        ent = persona._entry(self.KEY) or {}
        self.assertEqual(ent.get("outfit"), OUTFIT)
        # 回退靠 mood_prev；老快照没有这个键时 rollback 不会误清空。
        self.assertEqual((ent.get("mood_prev") or {}).get("outfit"), PREV)

    def test_missing_outfit_keeps_previous_value(self):
        self._seed(outfit=PREV)
        persona._save_mood(self.KEY, "害羞", 6, "卧室", "")
        self.assertEqual((persona._entry(self.KEY) or {}).get("outfit"), PREV)

    def test_injection_block_is_a_fact_not_a_gate(self):
        block = persona._outfit_block(PREV)
        self.assertIn(PREV, block)
        self.assertIn("照抄", block)
        # 判断交给模型：不列「脱下 / 穿上」这类换装词表，也不堆禁令。
        for word in ("脱下", "脱掉", "穿上", "不许", "不要"):
            self.assertNotIn(word, block)
        self.assertEqual(persona._outfit_block(""), "")


class BuildPromptTest(OutfitBase):
    def test_outfit_precedes_scene_and_fixed_outfit_retires(self):
        card = {"appearancePrompt": "1girl, blonde hair",
                "outfitPrompt": "oversized grey track jacket over a white shirt"}
        p = persona.build_prompt(card, "medium shot, smiling",
                                 pov=True, outfit="white shirt, no jacket")
        self.assertNotIn("oversized grey track jacket", p)
        self.assertIn("1girl, blonde hair", p)
        self.assertLess(p.index("white shirt, no jacket"),
                        p.index("medium shot, smiling"))
        self.assertTrue(p.endswith(", " + persona._POV_CAMERA))

    def test_legacy_callers_keep_the_old_shape(self):
        """galgame / 老单测不传 outfit：outfitPrompt 照拼，顺序照旧。"""
        card = {"appearancePrompt": "1girl, white hair",
                "outfitPrompt": "black dress"}
        self.assertEqual(persona.build_prompt(card, "sitting, smile"),
                         "1girl, white hair, black dress, sitting, smile")

    def test_contract_requires_four_meta_lines_in_both_variants(self):
        for text in (persona._CONTRACT, persona._CONTRACT_NO_INTIMATE):
            self.assertIn("服饰：", text)
            self.assertIn("服饰（这一行是她此刻穿着的定稿）", text)
            self.assertIn("补上四行元数据", text)


class ShotOutfitTest(OutfitBase):
    KEY = "web_outfit_test__card"

    def test_latest_turn_reads_the_outfit_line(self):
        with mock.patch.object(persona, "_load_hist", return_value=[
                {"role": "assistant", "content": RAW}]):
            self.assertEqual(persona.shot_outfit(self.KEY), OUTFIT)

    def test_archived_turn_reads_that_turns_outfit(self):
        rows = [{"role": "assistant", "content": RAW}]
        with mock.patch.object(persona, "load_archive", return_value=rows):
            self.assertEqual(persona.shot_outfit(self.KEY, 0), OUTFIT)

    def test_old_lines_fall_back_to_current_state(self):
        persona._save_state({self.KEY: {"card": "card", "outfit": PREV}})
        old = "她笑了。\n\n画面：close-up\n心情：开心 强度3\n状态：家里\n"
        with mock.patch.object(persona, "_load_hist", return_value=[
                {"role": "assistant", "content": old}]):
            self.assertEqual(persona.shot_outfit(self.KEY), PREV)


if __name__ == "__main__":
    unittest.main()
