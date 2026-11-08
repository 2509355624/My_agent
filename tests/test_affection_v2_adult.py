# -*- coding: utf-8 -*-
"""v2 成人向状态条结算器（app/affection_v2_adult.py）的契约测试。

这些测试钉的是**用户拍板的规则**，不是实现细节：
  · 冲动是档位，开发度 < 40 时最高 2 档
  · 高潮到顶回落 reset_to，可以继续涨
  · 堕落/屈服在正向好感时恒不涨
  · 敏感度恒 >= 开发度（开发 80 敏感 20 不存在）
  · 敏感度只从前戏/道具涨，单纯插入只涨开发度
  · 羞耻 0~50 正常亲密，50 以上只有露出类事件能推
  · 羞耻同类事件重复做，涨幅递减到 0
  · 主动权 0~100，50 对等
  · 接触是枚举，只记事实
"""

import unittest

from app import affection_v2_adult as ad


class ConfigTest(unittest.TestCase):
    def test_config_file_loads(self):
        cfg = ad._cfg()
        for key in ("arousal", "excite", "corrupt", "submit",
                    "dev", "sense", "shame", "control", "contact"):
            self.assertIn(key, cfg, "缺轴 %s" % key)
            self.assertTrue(cfg[key].get("levels"), "%s 没有档位" % key)

    def test_arousal_is_tier_not_number(self):
        cfg = ad._cfg()
        self.assertEqual(cfg["arousal"]["type"], "tier")
        tiers = [lv.get("tier") for lv in cfg["arousal"]["levels"]]
        self.assertEqual(tiers, sorted(tiers), "档位序号要升序")
        self.assertEqual(len(tiers), 5)

    def test_contact_is_enum(self):
        cfg = ad._cfg()
        self.assertEqual(cfg["contact"]["type"], "enum")
        self.assertEqual(set(cfg["contact"]["values"]),
                         {"none", "mouth", "pussy", "anus", "hand", "breast", "leg"})


class InitTest(unittest.TestCase):
    def test_empty_state_defaults(self):
        st = ad.empty_state()
        self.assertEqual(st["arousal"], "calm")
        self.assertEqual(st["excite"], 0)
        self.assertEqual(st["corrupt"], 0)
        self.assertEqual(st["submit"], 0)
        self.assertEqual(st["shame"], 20)
        self.assertEqual(st["control"], 50)
        self.assertEqual(st["contact"], "none")
        for p in ad.PARTS:
            self.assertEqual(st["dev"][p], 0)
            self.assertEqual(st["sense"][p], 0)

    def test_load_fills_missing(self):
        st = ad.load({})
        self.assertEqual(st["arousal"], "calm")
        st = ad.load({"adult": {"excite": 30}})
        self.assertEqual(st["excite"], 30)
        self.assertEqual(st["control"], 50)          # 缺的补默认

    def test_save_roundtrip(self):
        rec = {}
        st = ad.normalize({**ad.empty_state(), "excite": 42, "contact": "pussy"})
        ad.save(rec, st)
        again = ad.load(rec)
        self.assertEqual(again["excite"], 42)
        self.assertEqual(again["contact"], "pussy")


class ArousalGateTest(unittest.TestCase):
    def test_low_dev_caps_arousal_at_tier2(self):
        st = ad.empty_state()                        # dev 全 0
        out, notes = ad.apply(st, {"arousal": "reverse"}, value=10)
        self.assertEqual(out["arousal"], "want", "开发度低时最多到「想要」")
        self.assertTrue(any("闸门" in n for n in notes))

    def test_high_dev_allows_reverse(self):
        st = ad.normalize({**ad.empty_state(),
                           "dev": {p: 80 for p in ad.PARTS},
                           "sense": {p: 80 for p in ad.PARTS}})
        out, notes = ad.apply(st, {"arousal": "reverse"}, value=10)
        self.assertEqual(out["arousal"], "reverse")

    def test_dev_avg_is_used(self):
        # 只有一处高、其余 0，均值 16 < 40，仍然封顶
        st = ad.normalize({**ad.empty_state(), "dev": {"pussy": 80}})
        out, _ = ad.apply(st, {"arousal": "lead"}, value=10)
        self.assertEqual(out["arousal"], "want")
        self.assertLess(ad.dev_avg(st), 40)


class ExciteTest(unittest.TestCase):
    def test_climb_and_reset(self):
        st = ad.normalize({**ad.empty_state(), "excite": 85})
        out, _ = ad.apply(st, {"excite": 20}, value=10)
        self.assertEqual(out["excite"], 50, "到顶后回落 reset_to")
        self.assertEqual(out["orgasm_count"], 1)

    def test_can_rise_again_after_reset(self):
        st = ad.normalize({**ad.empty_state(), "excite": 50})
        out, _ = ad.apply(st, {"excite": 30}, value=10)
        self.assertEqual(out["excite"], 80)

    def test_orgasm_flag_forces_reset(self):
        st = ad.normalize({**ad.empty_state(), "excite": 60})
        out, _ = ad.apply(st, {"orgasm": True}, value=10)
        self.assertEqual(out["excite"], 50)
        self.assertEqual(out["orgasm_count"], 1)


class GateTest(unittest.TestCase):
    def test_positive_value_blocks_corrupt_and_submit(self):
        st = ad.empty_state()
        out, notes = ad.apply(st, {"corrupt": 30, "submit": 30}, value=40)
        self.assertEqual(out["corrupt"], 0)
        self.assertEqual(out["submit"], 0)
        self.assertEqual(len(notes), 2)

    def test_zero_value_blocks(self):
        st = ad.empty_state()
        out, _ = ad.apply(st, {"corrupt": 10, "submit": 10}, value=0)
        self.assertEqual(out["corrupt"], 0)
        self.assertEqual(out["submit"], 0)

    def test_negative_value_allows_growth(self):
        st = ad.empty_state()
        out, notes = ad.apply(st, {"corrupt": 20, "submit": 15}, value=-10)
        self.assertEqual(out["corrupt"], 20)
        self.assertEqual(out["submit"], 15)
        self.assertEqual(notes, [])

    def test_gain_only_clamps_at_max(self):
        st = ad.normalize({**ad.empty_state(), "corrupt": 95, "submit": 95})
        out, _ = ad.apply(st, {"corrupt": 20, "submit": 20}, value=-10)
        self.assertEqual(out["corrupt"], 100)
        self.assertEqual(out["submit"], 100)


class DevSenseTest(unittest.TestCase):
    def test_dev_only_grows(self):
        st = ad.normalize({**ad.empty_state(), "dev": {"pussy": 50}})
        out, _ = ad.apply(st, {"dev": {"pussy": -20}}, value=10)
        self.assertEqual(out["dev"]["pussy"], 50, "开发度只涨不掉")

    def test_sense_never_below_dev(self):
        st = ad.normalize({**ad.empty_state(),
                           "dev": {"pussy": 80},
                           "sense": {"pussy": 20}})
        self.assertEqual(st["sense"]["pussy"], 80,
                         "开发 80 敏感 20 是不存在的，自动抬到 80")

    def test_sense_drops_on_orgasm(self):
        st = ad.normalize({**ad.empty_state(),
                           "dev": {p: 50 for p in ad.PARTS},
                           "sense": {p: 90 for p in ad.PARTS},
                           "excite": 60})
        out, _ = ad.apply(st, {"orgasm": True}, value=10)
        for p in ad.PARTS:
            self.assertEqual(out["sense"][p], 80, "高潮后整片回落 10")
            self.assertGreaterEqual(out["sense"][p], out["dev"][p])

    def test_plain_sex_does_not_raise_sense(self):
        """单纯插入只推开发度——敏感度靠前戏/道具（用户 2026-11-08 明确）。"""
        st = ad.empty_state()
        out, _ = ad.apply(st, {"dev": {"pussy": 10}}, value=10)
        self.assertEqual(out["dev"]["pussy"], 10)
        # 敏感度被 floor 规则抬到 dev（0→10），但不会高过它——
        # 「单纯插入推的是开发度，不是敏感度」在这里体现为「不额外涨」。
        self.assertEqual(out["sense"]["pussy"], out["dev"]["pussy"])
        # 再插一轮，dev 到 20，sense 跟着到 20，仍然不多涨
        out2, _ = ad.apply(out, {"dev": {"pussy": 10}}, value=10)
        self.assertEqual(out2["dev"]["pussy"], 20)
        self.assertEqual(out2["sense"]["pussy"], 20)


class ShameTest(unittest.TestCase):
    def test_soft_cap_blocks_normal_events(self):
        st = ad.normalize({**ad.empty_state(), "shame": 50})
        out, notes = ad.apply(st, {"shame": 10, "shame_event": "pussy_sex"}, value=20)
        self.assertEqual(out["shame"], 50)
        self.assertTrue(any("软顶" in n for n in notes))

    def test_exposure_can_pass_soft_cap(self):
        st = ad.normalize({**ad.empty_state(), "shame": 50})
        out, notes = ad.apply(st, {"shame": 10, "shame_event": "outdoor_exposed"}, value=20)
        self.assertEqual(out["shame"], 60)
        self.assertEqual(notes, [])

    def test_repeat_event_decays(self):
        st = ad.normalize({**ad.empty_state(), "shame": 20})
        gains = []
        for _ in range(4):
            st, _ = ad.apply(st, {"shame": 10, "shame_event": "kiss"}, value=20)
            gains.append(st["shame"])
        self.assertEqual(gains[0] - 20, 10)          # 第 1 次全额
        self.assertEqual(gains[1] - gains[0], 5)     # 第 2 次 -5
        self.assertEqual(gains[2] - gains[1], 0)     # 第 3 次没了
        self.assertEqual(gains[3], gains[2])         # 之后不再涨

    def test_below_soft_cap_normal_events_work(self):
        st = ad.normalize({**ad.empty_state(), "shame": 30})
        out, _ = ad.apply(st, {"shame": 10, "shame_event": "pussy_sex"}, value=20)
        self.assertEqual(out["shame"], 40)


class ControlTest(unittest.TestCase):
    def test_neutral_is_50(self):
        cfg = ad._cfg()
        self.assertEqual(cfg["control"]["initial"], 50)
        self.assertEqual(cfg["control"]["neutral"], 50)

    def test_delta_moves_both_ways(self):
        st = ad.empty_state()
        out, _ = ad.apply(st, {"control": -30}, value=10)
        self.assertEqual(out["control"], 20)
        out, _ = ad.apply(out, {"control": 40}, value=10)   # 接着上一轮
        self.assertEqual(out["control"], 60)
        out, _ = ad.apply(out, {"control": 100}, value=10)
        self.assertEqual(out["control"], 100)


class ContactTest(unittest.TestCase):
    def test_valid_enum_accepted(self):
        st = ad.empty_state()
        for v in ("mouth", "pussy", "anus", "hand", "breast", "leg", "none"):
            out, _ = ad.apply(st, {"contact": v}, value=10)
            self.assertEqual(out["contact"], v)

    def test_invalid_value_ignored(self):
        st = ad.normalize({**ad.empty_state(), "contact": "pussy"})
        out, _ = ad.apply(st, {"contact": "banana"}, value=10)
        self.assertEqual(out["contact"], "pussy", "枚举外的值不写进去")


class BlockTest(unittest.TestCase):
    def test_empty_state_gives_minimal_block(self):
        blk = ad.block(ad.empty_state())
        self.assertIn("她身上这会儿", blk)
        self.assertNotIn("她身体比嘴先服", blk, "堕落 0 时不该出现")

    def test_block_uses_chinese_only_no_field_names(self):
        st = ad.normalize({**ad.empty_state(), "arousal": "want", "excite": 60,
                           "shame": 55, "control": 70,
                           "dev": {"pussy": 70}, "sense": {"pussy": 85},
                           "contact": "pussy"})
        blk = ad.block(st)
        for bad in ("arousal", "excite", "shame", "control", "dev", "sense"):
            self.assertNotIn(bad, blk, "注入块里不许出现字段名")
        self.assertIn("小穴", blk)
        self.assertIn("她这会儿想不想", blk)

    def test_untouched_parts_not_listed(self):
        st = ad.normalize({**ad.empty_state(), "dev": {"pussy": 60},
                           "sense": {"pussy": 60}})
        blk = ad.block(st)
        self.assertIn("小穴", blk)
        self.assertNotIn("后庭", blk, "没碰过的地方不占篇幅")


class PanelTest(unittest.TestCase):
    def test_panel_shape(self):
        p = ad.panel(ad.empty_state())
        self.assertIn("rows", p)
        self.assertIn("parts", p)
        axes = [r["axis"] for r in p["rows"]]
        for a in ("arousal", "excite", "corrupt", "submit", "shame", "control"):
            self.assertIn(a, axes)
        self.assertEqual(len(p["parts"]), 5)

    def test_arousal_row_is_tier_typed(self):
        p = ad.panel(ad.empty_state())
        row = [r for r in p["rows"] if r["axis"] == "arousal"][0]
        self.assertEqual(row["type"], "tier")
        self.assertTrue(row["gateActive"], "dev 0 时闸门是开着的")

    def test_shame_row_exposes_soft_cap(self):
        p = ad.panel(ad.empty_state())
        row = [r for r in p["rows"] if r["axis"] == "shame"][0]
        self.assertEqual(row["softCap"], 50)


class ScorePromptTest(unittest.TestCase):
    """编剧提示词里必须有的成人向字段（钉住契约，防止哪天被顺手删掉）。"""

    def test_prompt_has_adult_block(self):
        from app import persona
        p = persona._SCORE_PROMPT_V2
        self.assertIn("\"adult\"", p)
        for key in ("arousal", "excite", "corrupt", "submit", "dev", "sense",
                    "shame", "shame_event", "control", "contact", "orgasm", "refuse"):
            self.assertIn(key, p, "提示词缺字段 %s" % key)

    def test_prompt_has_the_five_parts(self):
        from app import persona
        p = persona._SCORE_PROMPT_V2
        for part in ad.PARTS:
            self.assertIn(part, p, "五处缺 %s" % part)

    def test_prompt_states_the_gates(self):
        """闸门必须写在提示词里，否则编剧会一直给正向好感也加堕落。"""
        from app import persona
        p = persona._SCORE_PROMPT_V2
        self.assertIn("只在好感为负时", p)
        self.assertIn("单纯插入不推这里", p)
        self.assertIn("最多判到 want", p)

    def test_prompt_lists_shame_event_kinds(self):
        from app import persona
        p = persona._SCORE_PROMPT_V2
        for ev in ("kiss", "intimate_private", "outdoor", "exposed", "caught"):
            self.assertIn(ev, p, "羞耻事件类别缺 %s" % ev)

    def test_judge_v2_passes_adult_through(self):
        """_judge_v2 要把 adult 原样交出去，不在这一层净化。"""
        import inspect
        from app import persona
        src = inspect.getsource(persona._judge_v2)
        self.assertIn('score["adult"] = d["adult"]', src)


class RunTurnWiringTest(unittest.TestCase):
    """run_turn 要真的把成人向结算接上（v2 分支才走）。"""

    def test_run_turn_calls_adult_apply(self):
        import inspect
        from app import persona
        src = inspect.getsource(persona.run_turn)
        self.assertIn("adult_state.load", src)
        self.assertIn("adult_state.apply", src)
        self.assertIn("adult_state.block", src)
        self.assertIn("if v2 and", src, "必须只在 v2 分支走，不能污染 v1")

    def test_state_saved_after_settle(self):
        import inspect
        from app import persona
        src = inspect.getsource(persona.run_turn)
        self.assertIn("_save_state(_astate)", src)


class StateLocationTest(unittest.TestCase):
    """状态只存一处（2026-11-08 实测踩过：写顶层、读 affection，起点被盖成默认值）。"""

    def test_save_writes_under_affection(self):
        rec = {"affection": {"value": 50}}
        ad.save(rec, ad.normalize({**ad.empty_state(), "excite": 42}))
        self.assertIn("adult", rec["affection"])
        self.assertEqual(rec["affection"]["adult"]["excite"], 42)
        self.assertNotIn("adult", rec, "顶层不该再有副本")

    def test_load_reads_from_affection(self):
        rec = {"affection": {"adult": {"excite": 30, "dev": {"pussy": 60}}}}
        st = ad.load(rec)
        self.assertEqual(st["excite"], 30)
        self.assertEqual(st["dev"]["pussy"], 60)
        # 下限规则：sense 被抬到 dev
        self.assertEqual(st["sense"]["pussy"], 60)

    def test_load_falls_back_to_top_level(self):
        """极早期副本写在顶层，也要读得到（兼容，但不鼓励）。"""
        rec = {"adult": {"excite": 15}}
        self.assertEqual(ad.load(rec)["excite"], 15)

    def test_affection_wins_over_top_level(self):
        rec = {"affection": {"adult": {"excite": 70}},
               "adult": {"excite": 10}}
        self.assertEqual(ad.load(rec)["excite"], 70,
                         "affection 下面那份是权威")

    def test_save_then_load_roundtrip_keeps_start_values(self):
        """最容易出事的场景：设好起点 → 存 → 读回来必须一模一样。"""
        start = ad.normalize({
            **ad.empty_state(),
            "excite": 30, "shame": 40,
            "dev": {"mouth": 20, "nipple": 30, "pussy": 40, "anus": 0, "skin": 25},
            "sense": {"mouth": 20, "nipple": 35, "pussy": 45, "anus": 0, "skin": 25},
        })
        rec = {"affection": {}}
        ad.save(rec, start)
        back = ad.load(rec)
        for k in ("excite", "shame"):
            self.assertEqual(back[k], start[k], "%s 被改掉了" % k)
        for part in ad.PARTS:
            self.assertEqual(back["dev"][part], start["dev"][part])
            self.assertEqual(back["sense"][part], start["sense"][part])

    def test_run_turn_uses_save_helper(self):
        import inspect
        from app import persona
        src = inspect.getsource(persona.run_turn)
        self.assertIn("adult_state.save", src)
        self.assertNotIn('_rec["adult"] = _ast', src)


class ShameCapOverflowTest(unittest.TestCase):
    """软顶不能被一次性冲过（2026-11-08 实测踩过：48 + 6 冲到 54）。"""

    def test_single_gain_cannot_jump_over_cap(self):
        st = ad.normalize({**ad.empty_state(), "shame": 48})
        out, notes = ad.apply(st, {"shame": 6, "shame_event": "kiss"}, value=50)
        self.assertEqual(out["shame"], 50, "一次涨幅被截断到软顶，而不是冲过去")
        self.assertTrue(any("截断" in n for n in notes))

    def test_gain_exactly_fills_room(self):
        st = ad.normalize({**ad.empty_state(), "shame": 45})
        out, notes = ad.apply(st, {"shame": 5, "shame_event": "kiss"}, value=50)
        self.assertEqual(out["shame"], 50)
        self.assertEqual(notes, [], "刚好填满不算截断")

    def test_exposure_still_passes(self):
        st = ad.normalize({**ad.empty_state(), "shame": 48})
        out, _ = ad.apply(st, {"shame": 6, "shame_event": "outdoor"}, value=50)
        self.assertEqual(out["shame"], 54, "露出类不受软顶限制")


class PanelContractTest(unittest.TestCase):
    """后端交给前端的形状（前端一个数都不重算，全靠这份）。"""

    def _rec(self):
        st = ad.normalize({**ad.empty_state(), "arousal": "want", "excite": 62,
                           "shame": 55, "control": 72, "corrupt": 18, "submit": 10,
                           "dev": {"mouth": 30, "nipple": 55, "pussy": 70},
                           "sense": {"mouth": 35, "nipple": 60, "pussy": 85},
                           "contact": "pussy"})
        return {"schema": 2, "value": 45, "mode": "raid", "confess": 20,
                "contacts": [], "contact": 2, "day": 3, "turns": 12, "adult": st}

    def test_v2_panel_carries_adult(self):
        from app import affection
        p = affection._v2_panel(self._rec(), None)
        self.assertIn("adult", p)
        self.assertTrue(p["adult"]["rows"])

    def test_rows_have_every_axis(self):
        from app import affection
        p = affection._v2_panel(self._rec(), None)
        axes = [r["axis"] for r in p["adult"]["rows"]]
        for a in ("arousal", "excite", "corrupt", "submit", "shame", "control"):
            self.assertIn(a, axes)

    def test_arousal_row_is_tier(self):
        from app import affection
        p = affection._v2_panel(self._rec(), None)
        row = [r for r in p["adult"]["rows"] if r["axis"] == "arousal"][0]
        self.assertEqual(row["type"], "tier")
        self.assertEqual(row["name"], "想要")
        self.assertIn("maxTier", row)
        self.assertIn("gateActive", row)

    def test_shame_row_has_soft_cap(self):
        from app import affection
        p = affection._v2_panel(self._rec(), None)
        row = [r for r in p["adult"]["rows"] if r["axis"] == "shame"][0]
        self.assertEqual(row["softCap"], 50)

    def test_control_row_has_neutral(self):
        from app import affection
        p = affection._v2_panel(self._rec(), None)
        row = [r for r in p["adult"]["rows"] if r["axis"] == "control"][0]
        self.assertEqual(row["neutral"], 50)

    def test_corrupt_submit_marked_gated(self):
        from app import affection
        p = affection._v2_panel(self._rec(), None)
        for axis in ("corrupt", "submit"):
            row = [r for r in p["adult"]["rows"] if r["axis"] == axis][0]
            self.assertTrue(row.get("gated"), "%s 该标「好感为负才涨」" % axis)

    def test_parts_have_dev_and_sense(self):
        from app import affection
        p = affection._v2_panel(self._rec(), None)
        parts = p["adult"]["parts"]
        self.assertEqual(len(parts), 5)
        pussy = [x for x in parts if x["part"] == "pussy"][0]
        self.assertEqual(pussy["dev"], 70)
        self.assertEqual(pussy["sense"], 85)
        self.assertEqual(pussy["name"], "小穴")

    def test_contact_name_resolved(self):
        from app import affection
        p = affection._v2_panel(self._rec(), None)
        self.assertEqual(p["adult"]["contact"], "pussy")
        self.assertEqual(p["adult"]["contactName"], "小穴")

    def test_panel_survives_broken_adult(self):
        """成人向数据坏了，整页不该白屏——adult 返回 None，前端跳过这块。"""
        from app import affection
        rec = {"schema": 2, "value": 10, "mode": "raid", "adult": "垃圾数据"}
        p = affection._v2_panel(rec, None)
        self.assertIn("schema", p)          # 面板本身照常

    def test_frontend_reads_adult(self):
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", "persona.html")
        with io.open(path, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("p.adult", html, "前端要读成人向这块")
        self.assertIn("身体这条线", html)
        self.assertIn("五处（开发 / 敏感）", html)
        self.assertIn("好感为负才会涨", html)


class HelpModalTest(unittest.TestCase):
    """说明书弹窗：内容要真的讲清机制，入口要显眼。"""

    def _html(self):
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", "persona.html")
        with io.open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_modal_exists(self):
        h = self._html()
        self.assertIn('id="adultHelpMask"', h)
        self.assertIn("身体状态怎么看", h)

    def test_modal_covers_every_axis(self):
        h = self._html()
        for word in ("冲动", "高潮", "堕落", "屈服", "羞耻", "主动权",
                     "开发", "敏感", "接触"):
            self.assertIn(word, h, "说明书缺 %s" % word)

    def test_modal_marks_the_gates(self):
        """最容易玩错的几条要加高亮，否则玩家不知道 50 是软顶。"""
        h = self._html()
        self.assertIn("最高到「想要」", h)
        self.assertIn("掉回 50", h)
        self.assertIn("好感是负的", h)
        self.assertIn("50（软顶）", h)

    def test_modal_has_play_guide(self):
        h = self._html()
        self.assertIn("怎么玩起来", h)
        self.assertIn("先养开发", h)

    def test_entry_is_visible_in_panel(self):
        """入口不能是个藏起来的小问号，要带文字、虚线框、金色。"""
        h = self._html()
        self.assertIn("adultHelpBtn", h)
        self.assertIn("怎么看", h)
        self.assertIn("openAdultHelp", h)

    def test_modal_can_be_closed(self):
        h = self._html()
        self.assertIn("closeAdultHelp", h)
        self.assertIn('id="adultHelpClose"', h)
        self.assertIn("Escape", h)

    def test_not_left_open_by_default(self):
        """默认不能带 on —— 一进页面就弹说明书是灾难。"""
        h = self._html()
        import re
        m = re.search(r'<div class="skipMask([^"]*)" id="adultHelpMask"', h)
        self.assertIsNotNone(m)
        self.assertNotIn("on", (m.group(1) or "").split())


class AxisHelpTest(unittest.TestCase):
    """每条状态旁边的小问号：文案由配置给，前端只显示。"""

    def _panel(self):
        from app import affection, affection_v2_adult as ad
        rec = {"schema": 2, "value": 45, "mode": "raid", "confess": 20,
               "contacts": [], "contact": 2, "day": 3, "turns": 12,
               "adult": ad.normalize({**ad.empty_state(), "arousal": "want",
                                      "excite": 62, "shame": 55, "control": 72,
                                      "corrupt": 18, "submit": 10,
                                      "dev": {"pussy": 70}, "sense": {"pussy": 85},
                                      "contact": "pussy"})}
        return affection._v2_panel(rec, None)["adult"]

    def test_every_row_has_help(self):
        for r in self._panel()["rows"]:
            self.assertTrue(r.get("help"), "%s 缺 help" % r["axis"])
            self.assertTrue(r["help"].get("title"))
            self.assertTrue(r["help"].get("body"))

    def test_help_comes_from_config(self):
        """文案不能写死在前端——配置改了就跟着改。"""
        cfg = ad._cfg()
        for key in ("arousal", "excite", "corrupt", "submit",
                    "dev", "sense", "shame", "control", "contact"):
            h = (cfg.get(key) or {}).get("help")
            self.assertIsInstance(h, dict, "%s 配置里缺 help" % key)
            self.assertTrue(h.get("title"), "%s help 缺标题" % key)
            self.assertTrue(h.get("body"), "%s help 缺正文" % key)

    def test_parts_help_merges_dev_and_sense(self):
        ph = self._panel().get("partsHelp")
        self.assertTrue(ph)
        self.assertIn("开发", ph["title"])
        self.assertIn("敏感", ph["title"])
        self.assertGreaterEqual(len(ph["body"]), 4)

    def test_contact_help_present(self):
        self.assertTrue(self._panel().get("contactHelp"))

    def test_help_marks_the_gate(self):
        """最容易被玩错的那条，说明里必须点出来。"""
        rows = {r["axis"]: r for r in self._panel()["rows"]}
        joined = " ".join(rows["arousal"]["help"]["body"])
        self.assertIn("开发度不够", joined)
        joined = " ".join(rows["shame"]["help"]["body"])
        self.assertIn("50", joined)

    def test_frontend_renders_question_mark(self):
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", "persona.html")
        with io.open(path, encoding="utf-8") as fh:
            h = fh.read()
        self.assertIn('id="axisHelpMask"', h)
        self.assertIn("function axisQ", h)
        self.assertIn("function openAxisHelp", h)
        self.assertIn("axisHelpClose", h)
        # 粗体只认 **x**，先转义再替换——顺序写反就是注入
        self.assertIn("esc(line)", h)

    def test_axis_modal_not_open_by_default(self):
        import io
        import os
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", "persona.html")
        with io.open(path, encoding="utf-8") as fh:
            h = fh.read()
        m = re.search(r'<div class="skipMask([^"]*)" id="axisHelpMask"', h)
        self.assertIsNotNone(m)
        self.assertNotIn("on", (m.group(1) or "").split())


class RelationAxisHelpTest(unittest.TestCase):
    """关系轴那几行（好感/表白/病娇/接触档/在一起）也要有问号。"""

    def _panel(self):
        from app import affection, affection_v2_adult as ad
        rec = {"schema": 2, "value": 45, "mode": "raid", "confess": 20,
               "contacts": [], "contact": 2, "day": 3, "turns": 12,
               "adult": ad.normalize({**ad.empty_state(), "excite": 62,
                                      "shame": 55, "control": 72})}
        return affection._v2_panel(rec, None)

    def test_panel_carries_relation_help(self):
        h = self._panel().get("help")
        self.assertTrue(h, "面板要给关系轴的 help")
        for k in ("value", "confess", "yandere", "contacts", "proposal"):
            self.assertIn(k, h, "缺 %s 的说明" % k)
            self.assertTrue(h[k].get("title"))
            self.assertTrue(h[k].get("body"))

    def test_relation_help_comes_from_config(self):
        from app import affection
        cfg = affection._v2_config()
        for key in ("value", "confess", "yandere", "contacts", "proposal"):
            h = (cfg.get(key) or {}).get("help")
            self.assertIsInstance(h, dict, "%s 配置里缺 help" % key)

    def test_confess_help_explains_the_gate(self):
        """表白值是 30 分那道坎的关键，说明里必须点到。"""
        h = self._panel()["help"]["confess"]
        joined = " ".join(h["body"])
        self.assertIn("50", joined)
        self.assertIn("表白", joined)

    def test_contact_help_says_what_does_not_count(self):
        h = self._panel()["help"]["contacts"]
        joined = " ".join(h["body"])
        self.assertIn("拨头发", joined)

    def test_frontend_wires_relation_help(self):
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", "persona.html")
        with io.open(path, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("const H = p.help", html)
        for key in ('"value"', '"confess"', '"yandere"', '"contacts"', '"proposal"'):
            self.assertIn(key, html, "前端没接 %s 的问号" % key)


class PovTagAuditTest(unittest.TestCase):
    """扮演画面行的 POV 校验（复用漫画那套判据，2026-11-08）。"""

    def setUp(self):
        from app import comic_agent
        self.ca = comic_agent

    def _kinds(self, line):
        return [k for k, _, _ in self.ca.audit_line(line)]

    def test_solo_with_pov_hands_is_conflict(self):
        k = self._kinds("1girl, solo, pov, pov_hands, 1other, sitting")
        self.assertIn("solo_conflict", k)

    def test_pure_pov_without_hand_is_fine(self):
        self.assertEqual(self._kinds("1girl, solo, pov, looking at viewer"), [])

    def test_hand_around_wrist_is_banned(self):
        k = self._kinds("1girl, pov, pov_hands, 1other, hand around wrist")
        self.assertIn("banned_tag", k)

    def test_pov_hands_needs_1other(self):
        k = self._kinds("1girl, pov, pov_hands, sitting")
        self.assertIn("pov_direction", k)

    def test_1other_needs_pov_hands(self):
        k = self._kinds("1girl, pov, 1other, sitting")
        self.assertIn("pov_direction", k)

    def test_repair_drops_solo(self):
        fixed, ch = self.ca.repair_line(
            "1girl, solo, pov, holding another's wrist")
        self.assertNotIn("solo", fixed.split(", "))
        self.assertTrue(any("solo" in c for c in ch))

    def test_repair_adds_1other(self):
        fixed, ch = self.ca.repair_line("1girl, pov, pov_hands, sitting")
        parts = [x.strip() for x in fixed.split(",")]
        self.assertIn("1other", parts)

    def test_repair_replaces_banned(self):
        fixed, ch = self.ca.repair_line(
            "1girl, pov, pov_hands, 1other, hand around wrist")
        self.assertNotIn("hand around wrist", fixed)
        self.assertIn("holding another's wrist", fixed)

    def test_clean_line_untouched(self):
        s = "1girl, solo, pov, green hair, looking at viewer, soft light"
        fixed, ch = self.ca.repair_line(s)
        self.assertEqual(fixed, s)
        self.assertEqual(ch, [])

    def test_free_mode_skips_pov_rules(self):
        self.assertEqual(self.ca.audit_line("1girl, solo, pov, pov_hands",
                                            pov_mode="free"), [])


class PovContractTest(unittest.TestCase):
    """契约里必须写清 POV 的三种状态和两条硬禁令。"""

    def test_contract_states_three_pov_modes(self):
        from app import persona
        c = persona._CONTRACT
        self.assertIn("画面里只有她", c)
        self.assertIn("你的手入画", c)
        self.assertIn("female_pov", c)

    def test_contract_bans_solo_with_hands(self):
        from app import persona
        c = persona._CONTRACT
        self.assertIn("`solo` 和 `pov hands`", c)

    def test_contract_bans_hand_around_wrist(self):
        from app import persona
        c = persona._CONTRACT
        self.assertIn("hand around wrist", c)
        self.assertIn("holding another's wrist", c)

    def test_contract_has_negative_anchor(self):
        from app import persona
        self.assertIn("no other hands in frame", persona._CONTRACT)

    def test_contract_bans_pulling_verbs(self):
        from app import persona
        c = persona._CONTRACT
        self.assertIn("拉扯动词", c)
        self.assertIn("already slipped off", c)

    def test_draw_calls_repair(self):
        import inspect
        from app import persona
        src = inspect.getsource(persona.draw)
        self.assertIn("repair_line", src)


class ItemSystemTest(unittest.TestCase):
    """道具系统（2026-11-08）：道具是「一段写法」，不是数值。"""

    def setUp(self):
        from app import items
        self.it = items

    def test_catalog_has_ten(self):
        self.assertGreaterEqual(len(self.it.all_items()), 10)

    def test_every_item_has_prompt_and_help(self):
        for key, it in self.it.all_items().items():
            self.assertTrue(it.get("prompt"), "%s 缺 prompt" % key)
            self.assertTrue(it.get("help"), "%s 缺 help" % key)
            self.assertTrue(it["help"].get("body"), "%s help 缺正文" % key)
            self.assertTrue(it.get("aliases"), "%s 缺别名" % key)

    def test_match_by_alias(self):
        self.assertEqual(self.it.match("我拿出跳蛋"), ["vibrator"])
        self.assertEqual(self.it.match("用点润滑"), ["lubricant"])

    def test_unknown_item_not_matched(self):
        self.assertEqual(self.it.match("我掏出个手雷"), [],
                         "表外的道具不该被匹配")

    def test_list_block_is_short(self):
        """常态注入只给名字——十几个道具全注 prompt 会炸预算。"""
        blk = self.it.list_block()
        self.assertIn("润滑液", blk)
        self.assertNotIn("挤出来是凉的", blk, "常态不该带 prompt")
        self.assertLess(len(blk), 400)

    def test_use_block_carries_prompt(self):
        blk = self.it.use_block(["sleep_drug"])
        self.assertIn("迷药", blk)
        self.assertIn("她闻到的时候已经晚了", blk)

    def test_use_block_skips_unknown(self):
        self.assertEqual(self.it.use_block(["手雷"]), "")

    def test_keepsake_block(self):
        blk = self.it.keepsake_block(
            [{"name": "钥匙扣", "kind": "keepsake", "from": "千夏", "day": 3}])
        self.assertIn("钥匙扣", blk)
        self.assertIn("千夏", blk)

    def test_keepsake_excludes_consumables(self):
        self.assertEqual(self.it.keepsake_block(
            [{"name": "酒", "kind": "consumable"}]), "")

    def test_gain_dedupes_same_item(self):
        rec = self.it.apply_gain([], [{"name": "钥匙扣", "kind": "keepsake",
                                       "from": "千夏"}])
        rec = self.it.apply_gain(rec, [{"name": "钥匙扣", "kind": "keepsake",
                                        "from": "千夏"}])
        self.assertEqual(len(rec), 1, "同一样东西不该记两次")

    def test_plot_items_flagged(self):
        self.assertTrue(self.it.is_plot("sleep_drug"))
        self.assertFalse(self.it.is_plot("lubricant"))

    def test_prompt_has_item_keys_placeholder(self):
        from app import persona
        self.assertIn("%(item_keys)s", persona._SCORE_PROMPT_V2)

    def test_run_turn_injects_items(self):
        import inspect
        from app import persona
        src = inspect.getsource(persona.run_turn)
        self.assertIn("_items.list_block", src)
        self.assertIn("_items.use_block", src)
        self.assertIn("_items.keepsake_block", src)

    def test_frontend_has_items_panel(self):
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", "persona.html")
        with io.open(path, encoding="utf-8") as fh:
            h = fh.read()
        self.assertIn('id="itemsMask"', h)
        self.assertIn('id="btnItems"', h)
        self.assertIn("function openItems", h)
        self.assertIn("function useItem", h)

    def test_api_route_exists(self):
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app", "persona_api.py")
        with io.open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('/api/persona/items', src)


class ItemLifecycleTest(unittest.TestCase):
    """道具生效与失效（2026-11-08）：一次只一个，代码倒计时，配置给时长。"""

    def setUp(self):
        from app import items
        self.it = items

    def test_every_item_has_turns_and_fade(self):
        for key, it in self.it.all_items().items():
            self.assertIn("turns", it, "%s 缺 turns" % key)
            self.assertIn("fade", it, "%s 缺 fade" % key)
            self.assertGreaterEqual(it["turns"], 1)

    def test_start_sets_full_countdown(self):
        act, old = self.it.start(None, "wine", turn_no=5)
        self.assertEqual(act["key"], "wine")
        self.assertEqual(act["left"], act["total"])
        self.assertEqual(old, "")

    def test_start_replaces_previous(self):
        act, _ = self.it.start(None, "wine")
        act2, old = self.it.start(act, "vibrator")
        self.assertEqual(act2["key"], "vibrator")
        self.assertEqual(old, "酒", "旧道具要能报出名字，好写收尾")

    def test_tick_counts_down(self):
        act, _ = self.it.start(None, "wine")
        total = act["total"]
        for i in range(total - 1):
            act, fade = self.it.tick(act)
            self.assertEqual(fade, "")
            self.assertIsNotNone(act)
        act, fade = self.it.tick(act)
        self.assertIsNone(act, "到 0 就该清空")
        self.assertEqual(fade, "酒")

    def test_turns_capped(self):
        """配置写成 99 也要被 MAX_TURNS 压住。"""
        self.assertLessEqual(self.it._turns_of("wine"), self.it.MAX_TURNS)

    def test_active_block_shows_round(self):
        act, _ = self.it.start(None, "wine")
        act, _ = self.it.tick(act)
        blk = self.it.active_block(act)
        self.assertIn("酒", blk)
        self.assertIn("第 2 轮", blk)

    def test_active_block_just_started_is_short(self):
        act, _ = self.it.start(None, "wine")
        blk = self.it.active_block(act, just_started=True)
        self.assertIn("第 1 轮", blk)
        self.assertNotIn("接着写", blk, "刚用的那一轮不重复 prompt")

    def test_fade_block_uses_config(self):
        blk = self.it.fade_block("酒", "wine")
        self.assertIn("酒", blk)
        self.assertIn("酒劲下去了", blk)

    def test_fade_block_empty_without_config(self):
        self.assertEqual(self.it.fade_block("不存在", "nonexistent"), "")

    def test_normalize_active_drops_invalid(self):
        self.assertIsNone(self.it.normalize_active(None))
        self.assertIsNone(self.it.normalize_active({"key": "不存在"}))
        self.assertIsNone(self.it.normalize_active({"key": "wine", "left": 0}))

    def test_normalize_active_keeps_valid(self):
        a = self.it.normalize_active({"key": "wine", "left": 2, "total": 3})
        self.assertEqual(a["key"], "wine")
        self.assertEqual(a["left"], 2)

    def test_run_turn_ticks_and_injects(self):
        import inspect
        from app import persona
        src = inspect.getsource(persona.run_turn)
        self.assertIn("_items.tick", src)
        self.assertIn("_items.start", src)
        self.assertIn("_items.active_block", src)
        self.assertIn("_items.fade_block", src)

    def test_fade_marker_is_popped_once(self):
        import inspect
        from app import persona
        src = inspect.getsource(persona.pop_item_fade)
        self.assertIn('pop("item_fade"', src)
        self.assertIn("_save_state", src)

    def test_api_returns_act_and_fade(self):
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app", "persona_api.py")
        with io.open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('out["act"]', src)
        self.assertIn('out["fade"]', src)

    def test_frontend_renders_item_line(self):
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", "persona.html")
        with io.open(path, encoding="utf-8") as fh:
            h = fh.read()
        self.assertIn("function itemLine", h)
        self.assertIn("生效中", h)
        self.assertIn("已失效", h)
        self.assertIn("line-through", h)


class ItemsFrontendTest(unittest.TestCase):
    """道具面板前端（2026-11-08 踩过：拼了个不存在的 KEY 变量，永远不渲染）。"""

    def _html(self):
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", "persona.html")
        with io.open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_fetch_does_not_use_undefined_key(self):
        """前端没有 KEY 这个变量；拼进去会抛 ReferenceError 被 catch 吞掉。"""
        h = self._html()
        import re
        m = re.search(r'fetch\("/api/persona/items[^"]*"\)', h)
        self.assertIsNotNone(m, "找不到道具接口的 fetch")
        self.assertEqual(m.group(0), 'fetch("/api/persona/items")',
                         "道具接口不该带 key 参数（后端从 cookie 取会话）")

    def test_fetch_has_error_message(self):
        h = self._html()
        self.assertIn("道具表没拉到", h, "拉取失败要给明确提示，不能只说「还没加载」")

    def test_all_items_have_use_button(self):
        """剧情道具也要能一键插入（2026-11-08 用户要求改的）。

        之前按「剧情道具要玩家自己写怎么用」做成不给按钮，用户实机反馈
        要能主动使用——改成全部可点，剧情道具插进来的模板多一个冒号，
        提示玩家补「怎么用 / 用在哪」。
        """
        h = self._html()
        self.assertNotIn('it.kind !== "plot"', h, "不该再把剧情道具排除在外")
        self.assertIn('it.kind === "plot"', h, "剧情道具要有单独的插入模板")

    def test_panel_renders_rows(self):
        h = self._html()
        self.assertIn('row.className = "itRow"', h)
        self.assertIn("KIND[it.kind]", h)


class ItemsUniversalTest(unittest.TestCase):
    """道具在 v1 / v2 / 自由模式三条路上都要能用（2026-11-08 补齐）。"""

    def test_item_keys_helper_exists(self):
        from app import persona
        txt = persona._item_keys_text()
        self.assertIn("lubricant=润滑液", txt)

    def test_v1_prompt_has_items_block(self):
        """v1 / 自由模式走 `_INTENT_PROMPT`，那里也要有 items 字段。"""
        from app import persona
        p = persona._INTENT_PROMPT
        self.assertIn('"items"', p)
        self.assertIn("%(item_keys)s", p)

    def test_v1_prompt_has_items_criteria(self):
        from app import persona
        p = persona._INTENT_PROMPT
        self.assertIn("真的用上了", p)
        self.assertIn("keepsake", p)

    def test_director_fields_passes_items(self):
        from app import persona
        out = persona._director_fields({"items": {"use": ["wine"]}})
        self.assertEqual(out.get("items"), {"use": ["wine"]})

    def test_director_fields_items_none_when_absent(self):
        from app import persona
        out = persona._director_fields({})
        self.assertIsNone(out.get("items"))

    def test_run_turn_reads_items_from_extras(self):
        """自由模式下 score 是 None，道具只能从 extras 拿。"""
        import inspect
        from app import persona
        src = inspect.getsource(persona.run_turn)
        self.assertIn("_sc = extras if isinstance(extras, dict)", src)

    def test_axis_help_above_items_panel(self):
        """点道具面板里的问号，说明卡不能被面板盖住（z-index 要更高）。"""
        import io
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", "persona.html")
        with io.open(path, encoding="utf-8") as fh:
            h = fh.read()
        self.assertIn("#axisHelpMask { z-index: 620; }", h)


if __name__ == "__main__":
    unittest.main()
