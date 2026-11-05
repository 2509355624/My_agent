# -*- coding: utf-8 -*-
"""开发度测试（2026-11-06 改版：满值 100、五阶段、五处 + 派生体质）。

玩法：嘴巴 / 乳头 / 小穴 / 肛门 / 肌肤 五处各自 0~100，**只涨不掉**；
每 20 一档（20/40/60/80/100）。「体质」不单独存储，是五处按权重折出来的，
所以某处刷满之后继续刷它，体质不会继续涨。

要钉住的四件事：
1. 五处 + 体质 的口径（体质 = 加权，不是求和）；
2. 敏感系数跟**体质**走，不是跟「最高的那一处」走；
3. 体质满格 = 一碰就溃（`dev_all_full`）；
4. 单轮单部位有上限，防模型一笔给爆。
"""

import unittest

import app.affection as affection
import app.persona as persona
import app.writing_skills as ws


def _full(v=100):
    return {k: v for k in affection.DEV_KEYS}


def _res(**kw):
    base = {"active": True, "value": 0, "level": 2, "tier": "touch",
            "levelName": "能抱了", "tierName": "肢体接触",
            "excite": 40, "arousal": 40, "corrupt": 0, "corruptDelta": 0,
            "submit": 0, "submitDelta": 0, "suspicion": 0,
            "suspicionDelta": 0, "cross": None, "allow": "", "applied": 0,
            "reason": ""}
    base.update(kw)
    return base


class DevNormTest(unittest.TestCase):
    def test_blank_has_five_spots(self):
        d = affection.dev_blank()
        self.assertEqual(set(d), {"mouth", "nipple", "pussy", "anus", "skin"})

    def test_chinese_keys_are_accepted(self):
        got = affection.norm_dev({"嘴巴": 10, "小穴": 20, "肛门": 5, "肌肤": 3})
        self.assertEqual(got["mouth"], 10)
        self.assertEqual(got["pussy"], 20)
        self.assertEqual(got["anus"], 5)
        self.assertEqual(got["skin"], 3)
        self.assertEqual(got["nipple"], 0)

    def test_old_body_key_maps_to_skin(self):
        """旧存档/旧提示词里的 `body` 一律归到「肌肤」。"""
        self.assertEqual(affection.norm_dev({"body": 7})["skin"], 7)

    def test_scalar_is_spread_evenly(self):
        got = affection.norm_dev(6)
        self.assertTrue(all(got[k] == 6 for k in affection.DEV_KEYS))

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(affection.dev_total(affection.norm_dev({"xx": 99})), 0)


class DevCurveTest(unittest.TestCase):
    def test_five_stages_start_at_one(self):
        """五档从 1 点起算：**只要动过 1 点，就不能再叫「没碰过」**。

        用户点名：数值已经变了、措辞还停在初始态，看着像没生效。
        """
        mins = sorted(l["min"] for l in affection.dev_levels() if l["min"] > 0)
        self.assertEqual(mins, [1, 20, 40, 60, 80])

    def test_stage_and_name(self):
        self.assertEqual(affection.dev_name(0), "没碰过")
        self.assertEqual(affection.dev_name(1), "起反应")
        self.assertEqual(affection.dev_name(12), "起反应")
        self.assertEqual(affection.dev_name(25), "会要了")
        self.assertEqual(affection.dev_name(45), "离不开")
        self.assertEqual(affection.dev_name(65), "阈值乱了")
        self.assertEqual(affection.dev_name(85), "彻底开发")
        self.assertEqual(affection.dev_name(100), "彻底开发")
        self.assertEqual(affection.dev_stage(0), 0)
        self.assertEqual(affection.dev_stage(45), 3)
        self.assertEqual(affection.dev_stage(100), 5)

    def test_body_is_weighted_not_summed(self):
        """体质 = 五处加权；只刷满一处，体质远不到满。"""
        one = {"pussy": 100}
        self.assertEqual(affection.body_dev(one), 30)
        self.assertEqual(affection.body_dev(_full(100)), 100)
        self.assertEqual(affection.body_dev({}), 0)

    def test_pushing_one_spot_past_max_does_not_raise_body(self):
        """用户点名的那条：某处满了继续刷它，体质不该再涨。"""
        a = {"pussy": 100, "mouth": 0, "nipple": 0, "anus": 0, "skin": 0}
        b = {"pussy": 100, "mouth": 40, "nipple": 0, "anus": 0, "skin": 0}
        self.assertEqual(affection.body_dev(a), 30)
        self.assertGreater(affection.body_dev(b), affection.body_dev(a),
                           "换地方开发才推得动体质")

    def test_sensitivity_follows_body(self):
        """敏感系数跟体质走——只刷小穴拉不满。"""
        self.assertAlmostEqual(affection.sensitivity({"pussy": 100}), 1.45,
                               places=2)
        self.assertAlmostEqual(affection.sensitivity(_full(100)), 2.5, places=2)

    def test_sensitivity_can_target_one_spot(self):
        dev = {"pussy": 100, "mouth": 0}
        self.assertAlmostEqual(affection.sensitivity(dev, "mouth"), 1.0)

    def test_after_value_climbs_with_body(self):
        self.assertEqual(affection.after_value({}), 55)
        self.assertEqual(affection.after_value(_full(100)), 75)

    def test_all_full_only_when_every_spot_is_100(self):
        self.assertTrue(affection.dev_all_full(_full(100)))
        self.assertFalse(affection.dev_all_full(_full(99)))

    def test_hook_ready_needs_40(self):
        self.assertFalse(affection.dev_hook_ready({"pussy": 39}))
        self.assertTrue(affection.dev_hook_ready({"pussy": 40}))

    def test_dev_text_carries_body(self):
        got = affection.dev_text({"mouth": 30, "pussy": 78})
        self.assertIn("嘴巴 30", got)
        self.assertIn("体质", got)


class DevSettleTest(unittest.TestCase):
    """接进 settle：只涨不掉、单轮有上限、压迫场涨更快。"""

    def setUp(self):
        import os
        import tempfile
        from unittest import mock
        import app.agents as agents
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
        persona.save_cards([{"name": "测试", "systemPrompt": "你是测试。"}])
        card = persona.find_card("测试")
        persona.switch_line("private_1", card)
        self.key = persona.resolve_line("private_1")
        affection.set_mode(self.key, True)

    def _settle(self, **score):
        sc = {"delta": 0, "turn_type": "action", "reason": "测试"}
        sc.update(score)
        return affection.settle(self.key, sc)

    def _seed_dev(self, dev):
        """把开发度直接写进**磁盘上的记录**（否则 settle 读到旧值）。"""
        ent = persona._entry(self.key)          # noqa: SLF001
        rec = ent["affection"]
        rec["dev"] = dict(dev)
        ent["affection"] = rec
        affection._save_entry(self.key, ent)    # noqa: SLF001

    def test_dev_grows_and_never_falls(self):
        r = self._settle(in_scene=True, act="hand", spot="pussy",
                         dev={"pussy": 6})
        self.assertGreater(r["dev"]["pussy"], 0)
        before = r["dev"]["pussy"]
        r = self._settle(in_scene=True, act="hand", spot="pussy",
                         dev={"pussy": -20})
        self.assertEqual(r["dev"]["pussy"], before)

    def test_single_round_is_capped(self):
        step = affection.dev_cfg()["step"]
        r = self._settle(in_scene=True, act="intense", spot="pussy",
                         dev={"pussy": 300})
        self.assertLessEqual(r["dev"]["pussy"], step)

    def test_no_contact_grows_nothing(self):
        r = self._settle(in_scene=False, act="none", dev={"pussy": 20})
        self.assertEqual(r["dev"]["pussy"], 0)

    def test_sensitivity_boosts_excite(self):
        low = self._settle(in_scene=True, act="hand")
        low_step = low["excite"] - affection.EXCITE_START

        self._seed_dev(_full(100))
        high = self._settle(in_scene=True, act="hand")
        high_step = high["excite"] - affection.ORGASM_AFTER

        self.assertGreater(low["sensitivity"], 0)
        self.assertGreater(high["sensitivity"], 1.0)
        self.assertGreater(high_step, low_step)

    def test_hook_is_dropped_below_60(self):
        r = self._settle(in_scene=True, act="hand", spot="pussy",
                         dev={"pussy": 10}, hook="她突然抓住你的手")
        self.assertEqual(r["devHook"], "")

    def test_hook_passes_once_high_enough(self):
        self._seed_dev({"mouth": 0, "nipple": 0, "pussy": 65,
                        "anus": 0, "skin": 0})
        r = self._settle(in_scene=True, act="hand", spot="pussy",
                         hook="公交上看别人搂抱，她开始夹腿")
        self.assertIn("公交", r["devHook"])


class DevInjectionTest(unittest.TestCase):
    def test_injection_lists_each_developed_spot(self):
        blk = affection.injection_block(_res(dev={
            "mouth": 30, "nipple": 0, "pussy": 78, "anus": 0, "skin": 0},
            devNames={"mouth": "起反应", "pussy": "离不开"}))
        self.assertIn("嘴巴 30", blk)
        self.assertIn("小穴 78", blk)
        self.assertIn("体质", blk)

    def test_hook_reaches_the_actor(self):
        blk = affection.injection_block(_res(
            dev={"pussy": 65}, devHook="她手指发凉，往你手心里钻"))
        self.assertIn("她手指发凉", blk)

    def test_all_full_adds_the_collapse_line(self):
        blk = affection.injection_block(_res(
            dev=_full(100), devAllFull=True))
        self.assertIn("已经没有一处是没被开发过的", blk)

    def test_all_full_plus_late_pregnancy_is_hotter(self):
        """体质满格 + 孕晚期：一碰就出水、一含就冒汁（用户点名）。"""
        blk = affection.injection_block(_res(
            dev=_full(100), devAllFull=True, pregStage="late"))
        self.assertIn("冒汁", blk)

    def test_writing_axis_switches_to_dev(self):
        got = ws.guide({"active": True, "dev": {"pussy": 65},
                        "corrupt": 10, "value": 0, "excite": 60})
        self.assertIn("差一步被发现", got)

    def test_forced_still_beats_dev(self):
        self.assertEqual(ws._top_axis({"active": True, "forced": True,
                                       "dev": {"pussy": 100}}),
                         "forced")


class DevTailTest(unittest.TestCase):
    def test_tail_line_shows_dev_and_body(self):
        rows = affection.tail_line(_res(dev={"mouth": 30, "pussy": 78},
                                        devNames={}, devPct=34, devMax=100))
        self.assertIn("开发 嘴巴 30", rows)
        self.assertIn("体质 34/100", rows)


if __name__ == "__main__":
    unittest.main()
