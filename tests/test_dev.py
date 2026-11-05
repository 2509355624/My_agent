# -*- coding: utf-8 -*-
"""开发度测试（2026-11-05，用户拍板）。

玩法：嘴巴 / 乳头 / 小穴 / 肛门 / 身体 五处各自一条，**不设上限，500 满格**。
涨多少由编剧 AI 按「这一轮动作的激烈程度」判，代码只做夹值、算敏感系数、
以及把状态交给演员层去演。

要钉住的四件事：
1. 只涨不掉、单轮单部位有上限（防模型一笔给爆）；
2. 敏感系数让同一个动作在开发度高时涨得更多；
3. 高潮次数不再封顶（第 6 次、第 7 次照记）；
4. 五处全满 = 身体开发 100%，注入里要出现「碰一下就溃」那一档。
"""

import unittest

import app.affection as affection
import app.persona as persona
import app.writing_skills as ws


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
    def test_blank_has_all_five_spots(self):
        d = affection.dev_blank()
        self.assertEqual(set(d), {"mouth", "nipple", "pussy", "anus", "body"})

    def test_chinese_keys_are_accepted(self):
        got = affection.norm_dev({"嘴巴": 10, "小穴": 20, "肛门": 5})
        self.assertEqual(got["mouth"], 10)
        self.assertEqual(got["pussy"], 20)
        self.assertEqual(got["anus"], 5)
        self.assertEqual(got["body"], 0)

    def test_scalar_is_spread_evenly(self):
        got = affection.norm_dev(6)
        self.assertTrue(all(got[k] == 6 for k in affection.DEV_KEYS))

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(affection.dev_total(affection.norm_dev({"xx": 99})), 0)


class DevCurveTest(unittest.TestCase):
    def test_sensitivity_rises_with_dev(self):
        self.assertAlmostEqual(affection.sensitivity({"pussy": 0}), 1.0)
        self.assertAlmostEqual(affection.sensitivity({"pussy": 250}), 1.75)
        self.assertAlmostEqual(affection.sensitivity({"pussy": 500}), 2.5)

    def test_sensitivity_uses_the_hottest_spot(self):
        dev = {"mouth": 0, "pussy": 500}
        self.assertAlmostEqual(affection.sensitivity(dev), 2.5)

    def test_after_value_climbs(self):
        """开发度越高越缓不下来：500 满格回到 75，不再回到 55。"""
        self.assertEqual(affection.after_value({"pussy": 0}), 55)
        self.assertEqual(affection.after_value({"pussy": 500}), 75)

    def test_stage_and_name(self):
        self.assertEqual(affection.dev_name(0), "没碰过")
        self.assertEqual(affection.dev_name(120), "起反应")
        self.assertEqual(affection.dev_name(310), "离不开")
        self.assertEqual(affection.dev_name(500), "开发满了")
        self.assertEqual(affection.dev_stage(0), 0)
        self.assertEqual(affection.dev_stage(310), 3)
        self.assertEqual(affection.dev_stage(500), 5)

    def test_all_full_only_when_every_spot_is_500(self):
        full = {k: 500 for k in affection.DEV_KEYS}
        self.assertTrue(affection.dev_all_full(full))
        full["anus"] = 499
        self.assertFalse(affection.dev_all_full(full))

    def test_hook_ready_needs_300(self):
        self.assertFalse(affection.dev_hook_ready({"pussy": 250}))
        self.assertTrue(affection.dev_hook_ready({"pussy": 300}))


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

    def _seed_dev(self, dev):
        """把开发度直接写进**磁盘上的记录**。

        不能改 `persona._entry(self.key)["affection"]` 的返回值——那是
        `_entry` 读出来的副本，改完不落盘，`settle` 下一次读到的还是旧值。
        落盘的辅助函数在 `affection` 里（`_save_entry`），不在 `persona`。
        """
        ent = persona._entry(self.key)          # noqa: SLF001
        rec = ent["affection"]
        rec["dev"] = dict(dev)
        ent["affection"] = rec
        affection._save_entry(self.key, ent)    # noqa: SLF001

    def _settle(self, **score):
        sc = {"delta": 0, "turn_type": "action", "reason": "测试"}
        sc.update(score)
        return affection.settle(self.key, sc)

    def test_dev_grows_and_never_falls(self):
        r = self._settle(in_scene=True, act="hand", spot="pussy",
                         dev={"pussy": 12})
        self.assertGreater(r["dev"]["pussy"], 0)
        before = r["dev"]["pussy"]
        # 模型给负数：开发度不接受负增量
        r = self._settle(in_scene=True, act="hand", spot="pussy",
                         dev={"pussy": -20})
        self.assertEqual(r["dev"]["pussy"], before)

    def test_single_round_is_capped(self):
        """模型一笔给爆（比如 300）时只吃单轮上限。"""
        step = affection.dev_cfg()["step"]
        r = self._settle(in_scene=True, act="intense", spot="pussy",
                         dev={"pussy": 300})
        self.assertLessEqual(r["dev"]["pussy"], step)

    def test_no_contact_grows_nothing(self):
        r = self._settle(in_scene=False, act="none", dev={"pussy": 20})
        self.assertEqual(r["dev"]["pussy"], 0)

    def test_sensitivity_boosts_excite(self):
        """开发度越高，同样的动作高潮值涨得越多。

        两条各自独立的线（低开发度 / 满开发度）跑一遍，比增量而不是绝对值——
        绝对值会被「已经在场里」的起始基准搅乱。
        """
        low = self._settle(in_scene=True, act="hand")
        low_step = low["excite"] - affection.EXCITE_START

        self._seed_dev({k: 500 for k in affection.DEV_KEYS})
        high = self._settle(in_scene=True, act="hand")
        high_step = high["excite"] - affection.ORGASM_AFTER

        self.assertGreater(low["sensitivity"], 0)
        self.assertGreater(high["sensitivity"], 1.0)
        self.assertGreater(high_step, low_step)

    def test_hook_is_dropped_below_300(self):
        r = self._settle(in_scene=True, act="hand", spot="pussy",
                         dev={"pussy": 20}, hook="她突然抓住你的手")
        self.assertEqual(r["devHook"], "")

    def test_hook_passes_once_high_enough(self):
        self._seed_dev({"mouth": 0, "nipple": 0, "pussy": 320,
                        "anus": 0, "body": 0})
        r = self._settle(in_scene=True, act="hand", spot="pussy",
                         hook="公交上看别人搂抱，她开始夹腿")
        self.assertIn("公交", r["devHook"])

    def test_missing_dev_falls_back_to_spot(self):
        """模型只给 spot、dev 整个丢掉时，按动作档位补一笔。

        2026-11-05 线上：打分 JSON 被截断，`dev` 在末尾经常丢，玩家明明
        写了「插小穴」，数值却一动不动。
        """
        r = self._settle(in_scene=True, act="thrust", spot="pussy")
        self.assertGreater(r["dev"]["pussy"], 0)

    def test_explicit_zero_is_respected(self):
        """模型明确给了 0（她躲开了）就不替它加分——补的是缺失，不是零。"""
        r = self._settle(in_scene=True, act="thrust", spot="pussy",
                         dev={"pussy": 0})
        self.assertEqual(r["dev"]["pussy"], 0)

    def test_fallback_still_capped(self):
        step = affection.dev_cfg()["step"]
        r = self._settle(in_scene=True, act="intense", spot="anus")
        self.assertLessEqual(r["dev"]["anus"], step)


class DevInjectionTest(unittest.TestCase):
    def test_injection_lists_each_developed_spot(self):
        blk = affection.injection_block(_res(dev={
            "mouth": 120, "nipple": 0, "pussy": 310, "anus": 0, "body": 0},
            devNames={"mouth": "起反应", "pussy": "离不开"}))
        self.assertIn("嘴巴 120", blk)
        self.assertIn("小穴 310", blk)
        self.assertIn("离不开", blk)

    def test_hook_reaches_the_actor(self):
        blk = affection.injection_block(_res(
            dev={"pussy": 320}, devHook="她手指发凉，往你手心里钻"))
        self.assertIn("她手指发凉", blk)

    def test_all_full_adds_the_collapse_line(self):
        blk = affection.injection_block(_res(
            dev={k: 500 for k in affection.DEV_KEYS}, devAllFull=True))
        self.assertIn("已经没有一处是没被开发过的", blk)

    def test_writing_axis_switches_to_dev(self):
        got = ws.guide({"active": True, "dev": {"pussy": 320},
                        "corrupt": 10, "value": 0, "excite": 0})
        self.assertIn("差一步被发现", got)

    def test_forced_still_beats_dev(self):
        """压迫场优先：那一场的写法不能被子轴的「日常发作」盖掉。"""
        self.assertEqual(ws._top_axis({"active": True, "forced": True,
                                       "dev": {"pussy": 500}}),
                         "forced")


class BustoNullTest(unittest.TestCase):
    def test_tail_line_shows_dev(self):
        rows = affection.tail_line(_res(dev={"mouth": 120, "pussy": 310},
                                        devNames={}, devPct=17, devMax=2500))
        self.assertIn("开发 嘴巴 120", rows)
        self.assertIn("小穴 310", rows)


if __name__ == "__main__":
    unittest.main()
