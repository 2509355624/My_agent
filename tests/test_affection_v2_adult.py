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


if __name__ == "__main__":
    unittest.main()
