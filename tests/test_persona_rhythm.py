# -*- coding: utf-8 -*-
"""节奏相关的两组机制（2026-11-09）。

一、原地打转的刻度：同一条线 84 轮里攒出 345 个跨轮复刻片段（早餐的葱油味、
    摊主问要点什么、隔着玻璃数了数反复出现）。原因是契约原来那条「同一处地方、
    镜头不换场地、手里不换物件」被模型读成「这一拍不许走」。
    代码只负责数「同一个地点连写了几拍」，换不换场仍由模型定。

二、写作 AI 读原话：编剧拆的「说 / 做 / 想」块在 2026-11-09 停止喂给写作 AI。
    编剧没有写作契约，拆错一个动作，写作 AI 就照着错的写。
"""
import unittest
from unittest import mock

from app import persona


class PlaceBlockTest(unittest.TestCase):
    """`_place_block` 只报数，不判换不换场。"""

    def test_quiet_before_three_beats(self):
        for gd in ({}, {"place_streak": 1}, {"place_streak": 2},
                   {"place_streak": "坏数据"}, None):
            self.assertEqual(persona._place_block(gd), "",
                             "三拍以内不该出现，白占一行预算")

    def test_reports_the_number_from_three_on(self):
        txt = persona._place_block({"place_streak": 3})
        self.assertIn("3 拍", txt)
        self.assertIn("已经连着写了", txt)
        txt9 = persona._place_block({"place_streak": 9})
        self.assertIn("9 拍", txt9)

    def test_offers_options_not_orders(self):
        """只能是事实 + 几个选项，不能变成「必须换场」的闸门。"""
        txt = persona._place_block({"place_streak": 5})
        for bad in ("必须", "不许", "禁止", "一定要换"):
            self.assertNotIn(bad, txt)
        # 就地接着写也是允许的
        self.assertIn("就地接着写没问题", txt)


class PlaceStreakTest(unittest.TestCase):
    """`_save_mood` 按状态行第一段（地点）累加/归零。"""

    def _save(self, state_line, gd):
        st = {"k": {"gameday": dict(gd)}}
        with mock.patch.object(persona, "_load_state", return_value=st), \
             mock.patch.object(persona, "_save_state"):
            persona._save_mood("k", "", 0, state_line)
        return st["k"]["gameday"]

    def test_same_place_adds_one(self):
        gd = self._save("早餐铺 · 白T恤 · 坐着 · 早晨 · 无",
                        {"place": "早餐铺", "place_streak": 2})
        self.assertEqual(gd["place_streak"], 3)

    def test_new_place_resets_to_one(self):
        gd = self._save("巷口 · 白T恤 · 站着 · 早晨 · 无",
                        {"place": "早餐铺", "place_streak": 7})
        self.assertEqual(gd["place_streak"], 1)
        self.assertEqual(gd["place"], "巷口")

    def test_first_known_place_starts_the_clock(self):
        """还不知道地点时只记下来，不凭空从 0 涨到 1。"""
        gd = self._save("教室 · 校服 · 坐着 · 上午 · 无", {})
        self.assertEqual(gd["place"], "教室")
        self.assertFalse(gd.get("place_streak"))

    def test_missing_state_line_leaves_it_alone(self):
        gd = self._save("", {"place": "教室", "place_streak": 4})
        self.assertEqual(gd["place_streak"], 4)
        self.assertEqual(gd["place"], "教室")


class RawUserTextTest(unittest.TestCase):
    """写作 AI 拿到的是原话；只有玩家自己标注的分栏才按块发。"""

    MULTI = ("我走过去，把她从地上拉起来\n"
             "「地上凉，起来。」\n"
             "我心里想着她手好凉")

    def test_multi_line_raw_text_keeps_the_whole_thing(self):
        blk = persona.render_user_block([], True, fallback=self.MULTI)
        # 三行原话一行不少
        self.assertIn("把她从地上拉起来", blk)
        self.assertIn("地上凉，起来。", blk)
        self.assertIn("我心里想着她手好凉", blk)
        # 有“这段话里混着三种东西”的说明，让模型自己拆
        self.assertIn("【玩家这一轮写的主输入框原文】", blk)
        self.assertIn("换行是他自己分的段", blk)
        # 场景义务跟着走
        self.assertIn(persona._SCENE_DUTY[:14], blk)

    def test_no_judge_segments_when_player_did_not_label(self):
        """编剧拆的块不该出现在写作 AI 的输入里。"""
        blk = persona.render_user_block([], True, fallback=self.MULTI)
        for tag in ("【这一轮你收到的", "用户 · 说（你听得见）",
                    "用户 · 想（心里想的"):
            self.assertNotIn(tag, blk)

    def test_single_line_stays_bare(self):
        blk = persona.render_user_block([], True, fallback="我走了。")
        self.assertEqual(blk, "我走了。")


if __name__ == "__main__":
    unittest.main()
