# -*- coding: utf-8 -*-
"""压迫模式测试（2026-11-05，用户拍板）。

玩法：**好感低 + 堕落高 + 气氛在动** 同时成立时，这一场不是「她愿意」，
是「她拦不住自己」。机制层要做的三件事：

1. `is_forced()` 三条件缺一不可——少一个就是普通越界，别乱扣帽子；
2. 堕落每跨一档，冲动门槛降一次——第一次要堆满气氛，第二次只要一半；
3. 注入换一套说法：「她挡得住」那句必须被顶掉，「别给干脆答案」也要被顶掉
   （压迫场里那句方向是反的，她必须有一个明确的拒绝）。
"""

import unittest

import app.affection as affection
import app.persona as persona
import app.writing_skills as ws


def _blank_cfg():
    """不带 forced 段的配置：验证兜底值，别让配置坏了就整根轴失效。"""
    cfg = dict(affection.DEFAULT_CONFIG)
    cfg = {k: v for k, v in cfg.items() if k != "forced"}
    return cfg


def _res(**kw):
    base = {"active": True, "value": 0, "level": 2, "tier": "touch",
            "levelName": "能抱了", "tierName": "肢体接触",
            "excite": 40, "arousal": 40, "corrupt": 60, "corruptDelta": 0,
            "submit": 0, "submitDelta": 0, "suspicion": 0, "suspicionDelta": 0,
            "cross": None, "allow": "", "applied": 0, "reason": ""}
    base.update(kw)
    return base


class ForcedThresholdTest(unittest.TestCase):
    def test_three_conditions_are_all_required(self):
        # 全齐
        self.assertTrue(affection.is_forced(0, 60, 40, 0))
        # 好感够高 → 不是压迫
        self.assertFalse(affection.is_forced(40, 60, 40, 0))
        # 堕落不够 → 她还没被弄坏，普通越界被拒
        self.assertFalse(affection.is_forced(0, 20, 40, 0))
        # 气氛没起来、也不在场景里 → 还没到那一步
        self.assertFalse(affection.is_forced(0, 60, 0, 0))

    def test_in_scene_counts_as_arousal(self):
        """已经在场景里（高潮值 > 0）就不用再堆冲动。"""
        self.assertTrue(affection.is_forced(0, 60, 0, 20))

    def test_negative_affection_is_still_low_affection(self):
        """负分档「她恨你」当然也算好感低。"""
        self.assertTrue(affection.is_forced(-30, 60, 40, 0))


class ForcedDiscountTest(unittest.TestCase):
    def test_corrupt_stages_climb(self):
        self.assertEqual(affection.corrupt_stage(0), 0)
        self.assertEqual(affection.corrupt_stage(25), 1)
        self.assertEqual(affection.corrupt_stage(60), 2)
        self.assertEqual(affection.corrupt_stage(95), 4)

    def test_threshold_drops_one_step_per_stage(self):
        fc = affection.forced_cfg()
        self.assertEqual(affection.forced_arousal_min(0), fc["arousal_min"])
        self.assertEqual(affection.forced_arousal_min(25),
                         fc["arousal_min"] - fc["discount"])
        # 降到底了就不再往下掉，别变成负数
        self.assertGreaterEqual(affection.forced_arousal_min(95), 0)

    def test_progress_reports_the_gap(self):
        """`on` 只说明「压迫那条路开着」（堕落够门槛），没开时不该有这一行。"""
        self.assertFalse(affection.forced_progress(0, 0)["on"])
        p = affection.forced_progress(45, 0)
        self.assertTrue(p["on"])
        self.assertEqual(p["gap"], p["need"])
        done = affection.forced_progress(60, 40)
        self.assertEqual(done["gap"], 0)

    def test_missing_config_falls_back(self):
        """配置里没有 forced 段时用兜底值，不能因为少一段就整根轴失效。"""
        fc = affection.forced_cfg(_blank_cfg())
        self.assertEqual(fc["corrupt_min"], 45)
        self.assertEqual(fc["arousal_min"], 30)
        self.assertEqual(list(fc["corrupt_steps"]), [25, 50, 70, 90])


class ForcedInjectionTest(unittest.TestCase):
    def test_forced_block_replaces_the_gate_line(self):
        blk = affection.injection_block(_res())
        self.assertIn("这一拍不是她愿意的", blk)
        self.assertNotIn("她挡得住", blk)

    def test_normal_block_still_says_she_can_hold(self):
        """不是压迫场时，原来那句必须原样在——别把普通线也改了。"""
        blk = affection.injection_block(_res(corrupt=10, arousal=5))
        self.assertIn("她挡得住", blk)
        self.assertNotIn("这一拍不是她愿意的", blk)

    def test_forced_overrides_the_no_plain_answer_rule(self):
        """「别给干脆答案」在压迫场里方向是反的：那时她必须有一个明确的拒绝。"""
        blk = affection.injection_block(_res())
        self.assertIn("主旋律是屈辱", blk)
        self.assertIn("必须有一个明确的、具体的拒绝", blk)
        self.assertIn("别写事后温存", blk)

    def test_forced_does_not_trigger_lead_initiative(self):
        """压迫场里不许「反客为主」——她自己先动就把屈辱写成甜线了。"""
        blk = affection.injection_block(_res(arousal=95, excite=90))
        self.assertNotIn("这一轮大概率由她先动", blk)

    def test_not_agreed_is_not_flirt(self):
        """「没答应也没走开」在压迫场里不是欲拒还迎，是拦不住。"""
        blk = affection.injection_block(_res(cross="mild", allow="hesitate"))
        self.assertIn("她没答应，也没能走开", blk)
        self.assertNotIn("这个状态最难看也最好看", blk)


class ForcedTailTest(unittest.TestCase):
    def test_tail_shows_the_gap_and_the_discount(self):
        rows = affection.tail_line(_res(corrupt=60, arousal=40))
        self.assertIn("压迫 现在就能开", rows)
        self.assertIn("已降 2 档", rows)

    def test_tail_hides_the_row_before_it_turns_on(self):
        rows = affection.tail_line(_res(corrupt=20, arousal=40))
        self.assertNotIn("压迫", rows)

    def test_tail_reports_remaining_arousal(self):
        rows = affection.tail_line(_res(corrupt=45, arousal=0))
        self.assertIn("压迫 差", rows)


class ForcedGuideTest(unittest.TestCase):
    def test_writing_axis_switches_to_forced(self):
        # excite 过 55 才进露骨层；低于它是涩气层，挑的是慢轴。
        got = ws.guide({"active": True, "forced": True, "corrupt": 60,
                        "value": 0, "excite": 60})
        self.assertIn("权力语言", got)
        self.assertIn("许可与断崖", got)

    def test_forced_axis_beats_the_tallest_axis(self):
        """屈服 90 也不该盖掉压迫——两件事的写法不是同一套。"""
        self.assertEqual(ws._top_axis({"active": True, "forced": True,
                                       "corrupt": 10, "submit": 90}),
                         "forced")

    def test_forced_gets_a_bigger_budget(self):
        # excite 过 55 才进露骨层——压迫场的三份材料只在那里拼。
        got = ws.guide({"active": True, "forced": True, "corrupt": 60,
                        "excite": 60})
        self.assertLessEqual(len(got), ws.FORCED_CHARS)
        self.assertGreater(len(got), ws.MAX_CHARS)

    def test_persona_layer_adds_the_compression_section(self):
        got = persona.intimate_guide({"active": True, "forced": True,
                                      "excite": 40, "corrupt": 60})
        self.assertIn("这一场是在压迫下发生的", got)
        self.assertIn("嘴和身体分开写", got)
        self.assertIn("不许她求欢", got)
        self.assertIn("别写事后温存", got)

    def test_no_compression_section_on_a_normal_line(self):
        got = persona.intimate_guide({"active": True, "excite": 40,
                                      "corrupt": 10, "value": 70})
        self.assertNotIn("这一场是在压迫下发生的", got)


if __name__ == "__main__":
    unittest.main()
