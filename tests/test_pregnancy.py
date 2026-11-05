# -*- coding: utf-8 -*-
"""怀孕 / 生产 / 周期（app/pregnancy.py）+ persona 接入。

用户 2026-11-05 定的口径：
- 从认识第 1 天起按 28 天排周期，安全期/易孕期/排卵期由代码判断；
- 只在**跨天**掷一次（时间跳跃 / 编剧判出过夜）；
- 妊娠 280 天，生产后孩子按天/岁/月显示；
- v1 不做流产/堕胎/未成年；现有成年卡默认开启，卡上可关。
"""

import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.persona as persona
import app.pregnancy as pregnancy


CFG = dict(pregnancy._DEFAULTS)                     # noqa: SLF001


def _ent():
    return {}


class CycleTest(unittest.TestCase):
    def test_phases_are_pure_function_of_day(self):
        cases = [
            (1, "safe", "安全期"),          # 用户：认识第 1 天就是安全期
            (5, "safe", "安全期"),
            (9, "safe", "安全期"),
            (10, "fertile", "易孕期"),
            (13, "ovulation", "排卵期"),
            (14, "ovulation", "排卵期"),
            (15, "ovulation", "排卵期"),
            (16, "fertile", "易孕期"),
            (17, "safe", "安全期"),
            (28, "safe", "安全期"),
            (29, "safe", "安全期"),         # 第二个周期的第 1 天
        ]
        for day, phase, name in cases:
            info = pregnancy.cycle_info(day, CFG)
            self.assertEqual(info["phase"], phase, day)
            self.assertEqual(info["phase_name"], name, day)
            self.assertEqual(info["cycle_day"], ((day - 1) % 28) + 1)

    def test_period_is_optional(self):
        """默认不加经期；想加就把 period_days 调大（配置项保留）。"""
        cfg = dict(CFG, period_days=5)
        self.assertEqual(pregnancy.cycle_info(1, cfg)["phase"], "period")
        self.assertEqual(pregnancy.cycle_info(6, cfg)["phase"], "safe")

    def test_safe_day_is_the_cheapest(self):
        self.assertEqual(pregnancy.cycle_info(7, CFG)["risk"],
                         CFG["chance_safe"])
        self.assertEqual(pregnancy.cycle_info(14, CFG)["risk"],
                         CFG["chance_ovulation"])

    def test_card_switch(self):
        with mock.patch.object(pregnancy, "load_config", return_value=CFG):
            self.assertTrue(pregnancy.card_enabled({"name": "甲"}))
            self.assertFalse(pregnancy.card_enabled(
                {"name": "甲", "pregnancyEnabled": False}))
            off = dict(CFG, enabled=False)
            with mock.patch.object(pregnancy, "load_config", return_value=off):
                self.assertFalse(pregnancy.card_enabled({"name": "甲"}))


class ExposureTest(unittest.TestCase):
    def test_records_and_accumulates_within_one_day(self):
        ent = _ent()
        self.assertTrue(pregnancy.record_inside(ent, 5))
        self.assertTrue(pregnancy.record_inside(ent, 5))
        self.assertEqual(ent["preg"]["exposures"], [{"day": 5, "n": 2}])

    def test_rejects_bad_days(self):
        ent = _ent()
        self.assertFalse(pregnancy.record_inside(ent, 0))
        self.assertFalse(pregnancy.record_inside(ent, "x"))

    def test_does_not_record_after_conception(self):
        ent = {"preg": {"status": "pregnant", "conceive_day": 5,
                        "due_day": 285}}
        self.assertFalse(pregnancy.record_inside(ent, 6))


class AdvanceTest(unittest.TestCase):
    def test_no_exposure_means_no_roll(self):
        ent = _ent()
        with mock.patch.object(pregnancy.random, "random", return_value=0.0):
            self.assertIsNone(pregnancy.advance(ent, 1, 2, CFG))
        # 只记「哪天查过」，不怀孕、不留暴露
        self.assertEqual(ent["preg"]["status"], "none")
        self.assertEqual(ent["preg"]["exposures"], [])
        self.assertEqual(ent["preg"]["last_checked_day"], 2)

    def test_safe_day_low_chance(self):
        ent = _ent()
        pregnancy.record_inside(ent, 7)             # 安全期 2%
        with mock.patch.object(pregnancy.random, "random", return_value=0.5):
            self.assertIsNone(pregnancy.advance(ent, 7, 8, CFG))
        self.assertEqual(ent["preg"]["exposures"], [])   # 掷过就清账

    def test_safe_day_can_still_conceive(self):
        ent = _ent()
        pregnancy.record_inside(ent, 7)
        with mock.patch.object(pregnancy.random, "random", return_value=0.01):
            self.assertEqual(pregnancy.advance(ent, 7, 8, CFG), "conceived")
        self.assertEqual(ent["preg"]["status"], "pregnant")
        self.assertEqual(ent["preg"]["conceive_day"], 7)
        self.assertEqual(ent["preg"]["due_day"], 7 + CFG["gestation_days"])

    def test_multiple_exposures_raise_but_stay_capped(self):
        ent = _ent()
        for _ in range(5):
            pregnancy.record_inside(ent, 8)
        # 安全期：0.02 + 0.02×4 = 0.10，封顶 0.08
        with mock.patch.object(pregnancy.random, "random", return_value=0.07):
            self.assertEqual(pregnancy.advance(ent, 8, 9, CFG), "conceived")
        ent2 = _ent()
        for _ in range(5):
            pregnancy.record_inside(ent2, 8)
        with mock.patch.object(pregnancy.random, "random", return_value=0.09):
            self.assertIsNone(pregnancy.advance(ent2, 8, 9, CFG))

    def test_pregnancy_births_at_due_day(self):
        ent = {"preg": {"status": "pregnant", "conceive_day": 1,
                        "due_day": 281}}
        self.assertIsNone(pregnancy.advance(ent, 280, 280, CFG))
        self.assertEqual(pregnancy.advance(ent, 280, 282, CFG), "birth")
        self.assertEqual(ent["preg"]["status"], "postpartum")
        self.assertEqual(ent["preg"]["born_day"], 281)
        self.assertEqual(len(ent["preg"]["children"]), 1)

    def test_long_jump_can_conceive_and_birth_in_one_call(self):
        ent = _ent()
        pregnancy.record_inside(ent, 1)
        with mock.patch.object(pregnancy.random, "random", return_value=0.0):
            self.assertEqual(pregnancy.advance(ent, 1, 400, CFG), "birth")
        self.assertEqual(ent["preg"]["born_day"], 1 + CFG["gestation_days"])

    def test_one_child_cap(self):
        ent = {"preg": {"status": "pregnant", "conceive_day": 1,
                        "due_day": 281}}
        pregnancy.advance(ent, 280, 282, CFG)
        self.assertEqual(len(ent["preg"]["children"]), 1)
        self.assertIsNone(pregnancy.advance(ent, 300, 400, CFG))


class ViewTest(unittest.TestCase):
    def test_not_exposed_when_card_is_off(self):
        self.assertEqual(pregnancy.view(_ent(), 5, enabled=False),
                         {"enabled": False, "status": "disabled"})

    def test_none_hides_cycle_details(self):
        v = pregnancy.view(_ent(), 5)
        self.assertEqual(v["status"], "none")
        self.assertNotIn("cycle_day", v)
        self.assertNotIn("risk", v)

    def test_pregnant_days_and_due(self):
        ent = {"preg": {"status": "pregnant", "conceive_day": 10,
                        "due_day": 290}}
        v = pregnancy.view(ent, 25)
        self.assertEqual(v["pregnant_days"], 16)
        self.assertEqual(v["days_left"], 265)

    def test_postpartum_child_age(self):
        ent = {"preg": {"status": "postpartum", "born_day": 100,
                        "children": [{"born_day": 100}]}}
        v = pregnancy.view(ent, 500)
        self.assertEqual(v["child_days"], 400)
        self.assertEqual(v["child_text"], "1 岁 1 个月 5 天")

    def test_child_age_text(self):
        self.assertEqual(pregnancy.child_age_text(0), "0 天")
        self.assertEqual(pregnancy.child_age_text(45), "1 个月 15 天")
        self.assertEqual(pregnancy.child_age_text(365), "1 岁")
        self.assertEqual(pregnancy.child_age_text(400), "1 岁 1 个月 5 天")

    def test_prompt_block_mentions_new_state(self):
        ent = {"preg": {"status": "pregnant", "conceive_day": 10,
                        "due_day": 290}}
        block = pregnancy.prompt_block(ent, 20, event="conceived")
        self.assertIn("已孕", block)
        self.assertIn("怀孕第 11 天", block)
        self.assertIn("新情况", block)

    def test_prompt_block_follows_pregnancy_stage(self):
        ent = {"preg": {"status": "pregnant", "conceive_day": 10,
                        "due_day": 290}}
        early = pregnancy.prompt_block(ent, 30)          # 怀孕第 21 天
        self.assertIn("早孕", early)
        self.assertIn("看不出来", early)
        mid = pregnancy.prompt_block(ent, 140)           # 怀孕第 131 天
        self.assertIn("孕中期", mid)
        self.assertIn("胎动", mid)
        late = pregnancy.prompt_block(ent, 270)          # 怀孕第 261 天
        self.assertIn("孕晚期", late)
        self.assertIn("水肿", late)
        for block in (early, mid, late):
            self.assertIn("肚子：", block)
            self.assertIn("激素与身体：", block)
            self.assertIn("她会主动：", block)

    def test_prompt_block_postpartum_stages(self):
        ent = {"preg": {"status": "postpartum", "born_day": 100,
                        "children": [{"born_day": 100}]}}
        fresh = pregnancy.prompt_block(ent, 103)
        self.assertIn("刚生产", fresh)
        self.assertIn("她会主动", fresh)
        self.assertIn("涨奶", fresh)
        self.assertIn("月子里", pregnancy.prompt_block(ent, 120))
        self.assertIn("已恢复", pregnancy.prompt_block(ent, 200))

    def test_clear_removes_everything(self):
        ent = {"preg": {"status": "postpartum", "children": [1]}, "x": 1}
        pregnancy.clear(ent)
        self.assertNotIn("preg", ent)
        self.assertEqual(ent["x"], 1)


class PersonaIntegrationTest(unittest.TestCase):
    """persona 侧只做两件事：记 inside、跨天时结算。"""

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

    def _card(self, on=True):
        card = {"id": "izumi", "name": "纱雾",
                "pregnancyEnabled": on}
        persona.save_cards([card])
        return persona.get_card("izumi")

    def test_record_inside_writes_today(self):
        key = "web_1__izumi"
        card = self._card()
        persona._save_state({key: {"card": "izumi", "gameday": {"day": 5}}})
        persona._preg_record(key, card, 5, "inside")
        ent = persona._entry(key)
        self.assertEqual(ent["preg"]["exposures"], [{"day": 5, "n": 1}])

    def test_record_skips_when_card_off(self):
        key = "web_1__izumi"
        card = self._card(on=False)
        persona._save_state({key: {"card": "izumi", "gameday": {"day": 5}}})
        persona._preg_record(key, card, 5, "inside")
        ent = persona._entry(key)
        self.assertNotIn("preg", ent)

    def test_other_finish_is_not_recorded(self):
        key = "web_1__izumi"
        card = self._card()
        persona._save_state({key: {"card": "izumi"}})
        persona._preg_record(key, card, 5, "outside")
        self.assertNotIn("preg", persona._entry(key))

    def test_later_finish_does_not_erase_today(self):
        """用户 2026-11-05：当天判过 inside，后面编剧再判 none 不能覆盖。"""
        key = "web_1__izumi"
        card = self._card()
        persona._save_state({key: {"card": "izumi", "gameday": {"day": 5}}})
        persona._preg_record(key, card, 5, "inside")
        persona._preg_record(key, card, 5, "none")
        persona._preg_record(key, card, 5, "outside")
        ent = persona._entry(key)
        self.assertEqual(ent["preg"]["exposures"], [{"day": 5, "n": 1}])

    def test_record_snapshots_state_before_exposure(self):
        key = "web_1__izumi"
        card = self._card()
        persona._save_state({key: {"card": "izumi", "gameday": {"day": 5}}})
        persona._preg_record(key, card, 5, "inside")
        ent = persona._entry(key)
        self.assertIn("preg_prev", ent)
        self.assertIsNone(ent["preg_prev"])       # 记录前没有怀孕状态

    def test_advance_conceives_on_cross_day(self):
        key = "web_1__izumi"
        card = self._card()
        persona._save_state({key: {"card": "izumi"}})
        persona._preg_record(key, card, 7, "inside")
        with mock.patch.object(pregnancy.random, "random", return_value=0.0):
            self.assertEqual(persona._preg_advance(key, 7, 8), "conceived")
        self.assertEqual(persona.pregnancy_status(key)["status"], "pregnant")

    def test_status_disabled_when_card_off(self):
        key = "web_1__izumi"
        self._card(on=False)
        persona._save_state({key: {"card": "izumi",
                                   "preg": {"status": "pregnant",
                                            "conceive_day": 1,
                                            "due_day": 281}}})
        self.assertEqual(persona.pregnancy_status(key)["status"], "disabled")

    def test_gameday_snapshots_pregnancy_for_rollback(self):
        key = "web_1__izumi"
        self._card()
        preg = {"status": "pregnant", "conceive_day": 1, "due_day": 281}
        persona._save_state({key: {"card": "izumi",
                                   "gameday": {"day": 10}, "preg": preg}})
        persona._apply_gameday(key, force_new_day=True, days=1)
        ent = persona._entry(key)
        self.assertEqual(ent["preg_prev"], preg)
        self.assertEqual(ent["gameday"]["day"], 11)

    def test_set_phase_keeps_day_and_turns(self):
        key = "web_1__izumi"
        self._card()
        persona._save_state({key: {
            "card": "izumi",
            "gameday": {"day": 3, "phase": "早晨", "turns_today": 5},
        }})
        got = persona.set_phase(key, "中午")
        self.assertEqual(got, {"day": 3, "phase": "中午"})
        ent = persona._entry(key)
        self.assertEqual(ent["gameday"]["phase"], "中午")
        self.assertEqual(ent["gameday"]["day"], 3)
        self.assertEqual(ent["gameday"]["turns_today"], 5)
        self.assertEqual(persona.game_phase(key), "中午")
        self.assertIsNone(persona.set_phase(key, "凌晨"))
        self.assertIsNone(persona.set_phase("web_2__izumi", "中午"))

    def test_wipe_clears_pregnancy_and_snapshot(self):
        key = "web_1__izumi"
        self._card()
        persona._save_state({key: {
            "card": "izumi", "gameday": {"day": 10},
            "preg": {"status": "pregnant", "conceive_day": 1, "due_day": 281},
            "preg_prev": {"status": "none"},
        }})
        persona._erase(key)
        ent = persona._entry(key) or {}
        self.assertNotIn("preg", ent)
        self.assertNotIn("preg_prev", ent)


if __name__ == "__main__":
    unittest.main()
