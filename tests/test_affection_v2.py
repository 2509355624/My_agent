"""v2 正向关系线的回归测试（2026-11-08）。

单独开一个文件，不碰 `tests/test_affection.py` 的既有基线——改前改后跑同一条
命令对比，才能分清哪些红项是本来就有的。

锁住五件事：
  · 两段式档位闸门：没表白就卡在「认准你了」，表白成功才进「在一起了」；
  · 身体接触是**事实记录**，不是分数，第一次单独留一条并写进注入；
  · 表白值 / 病娇值 / 主动表白触发，以及注入给演员的是中文自然语言；
  · 展开面板的数值上限与四张状态表来自 config，前端不重算；
    负向档不进表、整表只有当前档一个标记，绝不成排打勾；
  · 老档（没有 schema）一律不进 v2，读取前后零改动。
"""
import copy
import os
import re
import tempfile
import unittest
from unittest import mock

import app.affection as A
import app.agents as agents
import app.persona as P


def _rec(**kw):
    """一份干净的 v2 记录。"""
    r = {"mode": "raid", "value": 0, "tier": "normal", "level": 0,
         "arousal": 0, "turns": 0, "day": 0, "hist": [], "mercy": 3}
    r.update(A._v2_blank_fields())
    r["schema"] = 2
    r.update(kw)
    return r


def _settle(rec, score, day=1):
    """跑一次结算，落盘用 mock 挡掉（测试不碰真实 state）。"""
    ent = {"affection": rec}
    with mock.patch.object(A, "_save_entry"):
        res = A._v2_settle("t_v2_test", ent, rec, score, game_day=day)
    return res, rec


class TierGateTest(unittest.TestCase):
    """两段式：50 / 75 两档必须由表白成功解锁。"""

    def test_value_alone_never_unlocks_together(self):
        self.assertEqual(A._v2_tier(42, False)["name"], "认准你了")
        self.assertEqual(A._v2_tier(60, False)["name"], "认准你了")
        self.assertEqual(A._v2_tier(99, False)["name"], "认准你了")

    def test_confession_unlocks_together_then_lovers(self):
        self.assertEqual(A._v2_tier(60, True)["name"], "在一起了")
        self.assertEqual(A._v2_tier(80, True)["name"], "恋人")

    def test_order_of_relation_names(self):
        names = [t["name"] for t in sorted(
            A._v2_config()["tiers"], key=lambda x: int(x.get("min") or 0))
            if int(t.get("min") or 0) >= 0]
        self.assertEqual(names, ["正常关系", "有点在意", "动了心", "心里有你",
                                 "认准你了", "在一起了", "恋人"])


class ContactFactTest(unittest.TestCase):
    """身体接触：只记已发生的事实，不列许可、不跟分数走。"""

    def test_contact_comes_from_records_not_from_value(self):
        r = _rec(value=90, contacts=[{"degree": 1}, {"degree": 3}])
        lvl, deg = A._v2_contact_level(r)
        self.assertEqual(deg, 3)
        self.assertEqual(lvl["name"], "抱过")
        r2 = _rec(value=90, contacts=[])
        self.assertEqual(A._v2_contact_level(r2)[0]["name"], "还没碰过")

    def test_six_levels_are_facts_in_past_tense(self):
        names = [l["name"] for l in sorted(
            A._v2_config()["contacts"]["levels"],
            key=lambda x: int(x.get("degree") or 0))]
        self.assertEqual(names, ["还没碰过", "有过意外接触", "牵过手",
                                 "抱过", "亲过", "做过"])

    def test_degree_zero_does_not_write_a_record(self):
        r = _rec()
        score = {"delta": 5, "contact": {"degree": 0, "cause": "喂食"}}
        res, rec = _settle(r, score)
        self.assertEqual(rec["contacts"], [])
        self.assertEqual(res["contactName"], "还没碰过")

    def test_first_time_is_marked_and_reported_to_actor(self):
        r = _rec()
        score = {"contact": {"degree": 2, "place": "校门口",
                             "cause": "她主动", "excerpt": "她先握住了你的手"}}
        res, rec = _settle(r, score)
        self.assertIsNotNone(res["new_first"])
        self.assertEqual(res["new_first"]["name"], "牵过手")
        self.assertTrue(rec["contacts"][-1]["first"])
        self.assertIn("第一次", A._v2_injection(res))
        # 第二次同样的事不再算第一次
        res2, _ = _settle(rec, score)
        self.assertIsNone(res2["new_first"])

    def test_value_gain_does_not_advance_contact(self):
        r = _rec()
        res, rec = _settle(r, {"delta": 15})
        self.assertEqual(res["contactName"], "还没碰过")
        self.assertEqual(rec["contacts"], [])


class ConfessionTest(unittest.TestCase):
    """表白值、男生表白、女生主动表白、病娇事件。"""

    def test_proposal_success_sets_together(self):
        r = _rec(value=35)
        res, rec = _settle(r, {"proposal": "he", "accept": "yes"})
        self.assertTrue(res["just_together"])
        self.assertTrue(rec["together"])
        self.assertGreaterEqual(rec["value"], 50)
        self.assertIn("这一刻你们在一起了", A._v2_injection(res))

    def test_hesitate_is_not_a_success(self):
        r = _rec(value=35)
        res, rec = _settle(r, {"proposal": "he", "accept": "hesitate"})
        self.assertFalse(res["just_together"])
        self.assertFalse(rec["together"])

    def test_confess_gain_is_clamped_per_turn(self):
        r = _rec()
        _, rec = _settle(r, {"confess": 999})
        self.assertEqual(rec["confess"], 10)

    def test_auto_confess_triggers_once(self):
        r = _rec(value=45, confess=49)
        res, rec = _settle(r, {"confess": 5})
        self.assertTrue(res["auto_confess"])
        self.assertEqual(rec["confess"], 50)
        res2, _ = _settle(rec, {"confess": 5})
        self.assertFalse(res2["auto_confess"])

    def test_auto_confess_needs_both_axes(self):
        # 表白值满但感情线还没到认准你了
        r = _rec(value=26, confess=49)
        res, _ = _settle(r, {"confess": 5})
        self.assertFalse(res["auto_confess"])
        # 感情线到了但表白值没满
        r2 = _rec(value=45, confess=20)
        res2, _ = _settle(r2, {"confess": 5})
        self.assertFalse(res2["auto_confess"])

    def test_refusing_her_confession_raises_yandere(self):
        r = _rec(value=45, together=False)
        res, rec = _settle(r, {"refused_confession": True, "yandere": 20})
        self.assertGreaterEqual(rec["yandere"], 35)
        self.assertTrue(res["yandere_event"])


class InjectionTest(unittest.TestCase):
    """给演员的事实必须是中文自然语言，不能只扔英文字段名。"""

    def test_injection_explains_what_confess_means(self):
        r = _rec(value=26, confess=18)
        res, _ = _settle(r, {"delta": 3})
        txt = A._v2_injection(res)
        self.assertIn("她心里那句话", txt)
        self.assertIn("这不是分数解锁", txt)
        self.assertIn("不算接触", txt)
        self.assertNotIn("confess:", txt)
        self.assertNotIn("yandere:", txt)

    def test_second_actor_prompt_spells_out_the_axes(self):
        d = {"relation_name": "动了心", "relation_desc": "", "together": "",
             "contact_name": "有过意外接触", "confess": 18, "confess_max": 50,
             "confess_name": "想说", "confess_desc": "", "yandere_name": "没有",
             "recent": "", "gday": 1, "gphase": "下午", "gplan": "",
             "profile": "", "said": "走吧，回家吧", "history": "", "ledger": "",
             # 逐轮状态已从「她是谁」里拆出来，单独留在事实段（前缀缓存）
             "state_line": "",
             # 成人向状态块（2026-11-08 加）：提示词里多了这个占位，
             # 测试的 format 字典也得带上，否则 KeyError。
             "dev": "（五处都还没被开发过）",
             "item_keys": "（无）",
             "segment_guide": "", "facts_guide": "", "director_guide": ""}
        txt = P._SCORE_PROMPT_V2 % d
        self.assertIn("表白值", txt)
        self.assertIn("不是好感", txt)
        self.assertIn("拨头发", txt)
        self.assertIn("第一次", txt)
        # 病娇那一项必须写死「不给负值」，否则编剧照老习惯回吐负分
        self.assertIn("这一项不给负值", txt)
        self.assertIn("只涨不掉的棘轮", txt)

    def test_dynamic_block_is_silent_for_v2(self):
        self.assertEqual(A.dynamic_block({"active": True, "schema": 2}), "")

    def test_injection_says_together_is_a_done_deal(self):
        """已经是恋人：注入必须说「在一起了」，不能再给「还没开口」的提示。

        2026-11-09 用户实录：好感 100/恋人、表白值 50，正文还在拉扯
        「我们这样算什么」——因为注入里无论在不在一起都写着「该她开口了 /
        也不是她现在就要开口」，演员照着「还没挑明」演。
        """
        r = _rec(value=100, confess=50, together=True)
        res, _ = _settle(r, {})
        txt = A._v2_injection(res)
        self.assertIn("恋人", txt)
        self.assertIn("你们已经在一起了", txt)
        self.assertIn("已经说开了", txt)
        self.assertNotIn("也不是她现在就要开口", txt)

    def test_injection_keeps_the_two_stage_gate_when_not_together(self):
        """没表白时好感再高也只能读成「认准你了」，不能提前写成恋人。"""
        r = _rec(value=100, confess=50)
        res, _ = _settle(r, {})
        txt = A._v2_injection(res)
        self.assertIn("认准你了", txt)
        self.assertNotIn("—— 恋人", txt)
        self.assertIn("也不是她现在就要开口", txt)

    def test_tail_line_has_no_gate_numbers(self):
        r = _rec(value=26, confess=18)
        res, _ = _settle(r, {})
        tail = A.tail_line(res)
        self.assertIn("关系 动了心", tail)
        self.assertIn("已发生", tail)
        self.assertNotIn("/50", tail)


class YandereTest(unittest.TestCase):
    """病娇轴两件事（2026-11-09 用户拍板）：只涨不掉的棘轮 + 演法规则给演员。

    原来 `yandere` 能吃负值（哄回来就掉），但病娇的定义就是「她已经记下了」——
    能抹掉的分数演不出这一层。所以：
      1. 单轮增量先钳到 0 以上再累加，编剧给负值也掉不下去，上限照 max 钳；
      2. 文案在 `config/affection_v2.json` 的 `yandere.act`，病娇 >0 才整段递给
         第二段演员；配置里没有 `act` 就不给，不留一段空标题。
    """

    def test_negative_delta_never_lowers_yandere(self):
        _res, rec = _settle(_rec(yandere=30), {"yandere": -20})
        self.assertEqual(rec["yandere"], 30)
        # 连着一轮「哄好了 + 好感大涨」也一样抹不掉
        _res2, rec2 = _settle(rec, {"yandere": -5, "delta": 8})
        self.assertEqual(rec2["yandere"], 30)

    def test_positive_delta_raises_and_still_caps_at_max(self):
        _res, rec = _settle(_rec(yandere=30), {"yandere": 12})
        self.assertEqual(rec["yandere"], 42)
        _res2, rec2 = _settle(_rec(yandere=95), {"yandere": 20})
        self.assertEqual(rec2["yandere"], 100)

    def test_actor_gets_the_rules_once_she_is_actually_sick(self):
        res, _ = _settle(_rec(), {"yandere": 20})
        txt = A._v2_injection(res)
        self.assertIn("病娇这一层怎么演", txt)
        self.assertIn("占有欲", txt)
        # 档位读数那一行是另一件事，规则块不代替它
        self.assertIn("有点不安", txt)

    def test_no_rules_block_when_yandere_is_zero(self):
        res, _ = _settle(_rec(), {"yandere": -10})
        self.assertEqual(res["yandere"], 0)
        self.assertNotIn("病娇这一层怎么演", A._v2_injection(res))

    def test_empty_config_means_no_orphan_header(self):
        cfg = copy.deepcopy(A._v2_config())
        cfg["yandere"]["act"] = []
        self.assertEqual(A._v2_yandere_block(cfg, 60), "")
        self.assertTrue(A._v2_yandere_block(A._v2_config(), 60))

    def test_six_tiers_from_uneasy_to_merged(self):
        """六档顺序锁死：文案改档位名要连这里一起改，别让面板和注入对不上。"""
        stages = sorted(A._v2_config()["yandere"]["stages"],
                        key=lambda x: int(x.get("min") or 0))
        self.assertEqual([s["name"] for s in stages],
                         ["没有", "有点不安", "放不下", "锁人", "关起来", "融为一体"])
        self.assertEqual([int(s["min"]) for s in stages], [0, 20, 45, 75, 90, 100])

    def test_each_stage_line_carries_both_words_and_actions(self):
        """到 90 档：演员必须读到「反锁 / 钥匙」这类具体动作，不是一句「她绷着」。"""
        res, _ = _settle(_rec(yandere=75), {"yandere": 20})
        self.assertEqual(res["yandere"], 95)
        self.assertEqual(res["yandereName"], "关起来")
        txt = A._v2_injection(res)
        self.assertIn("反锁", txt)
        self.assertIn("我哪儿也不去", txt)


class PanelNumbersTest(unittest.TestCase):
    """展开面板：数值上限、四张状态表全来自 config，前端不重算。"""

    def test_limits_come_from_config_not_hardcoded(self):
        cfg = copy.deepcopy(A._v2_config())
        cfg["value"]["max"] = 120
        cfg["value"]["min"] = -30
        cfg["yandere"]["max"] = 80
        cfg["confess"]["max"] = 40
        with mock.patch.object(A, "_v2_config", return_value=cfg):
            p = A._v2_panel(_rec(value=26, confess=18))
        self.assertEqual(p["valueMax"], 120)
        self.assertEqual(p["valueMin"], -30)
        self.assertEqual(p["yandereMax"], 80)
        self.assertEqual(p["confessMax"], 40)
        self.assertEqual(p["contactMax"], 5)
        self.assertEqual(p["notContact"], cfg["contacts"]["not_contact"])

    def test_four_tables_are_ascending_with_one_current(self):
        p = A._v2_panel(_rec(value=26, confess=18,
                             contacts=[{"degree": 1, "first": True}]))
        for key in ("tierRows", "contactRows", "confessRows", "yandereRows"):
            rows = p[key]
            self.assertTrue(rows, key)
            self.assertEqual([r["gate"] for r in rows],
                             sorted(r["gate"] for r in rows), key)
            cur = [r for r in rows if r["state"] == "current"]
            self.assertEqual(len(cur), 1, key)
            # 整排 ✔ 是个真 bug：负向档门槛为负，好感 26 会被全判成 reached。
            self.assertFalse([r for r in rows if r["state"] == "reached"], key)
        self.assertEqual([r["name"] for r in p["confessRows"]
                          if r["state"] == "current"], ["开始在意"])
        self.assertEqual([r["name"] for r in p["contactRows"]
                          if r["state"] == "current"], ["有过意外接触"])

    def test_confess_row_becomes_confessed_after_together(self):
        """已经在一起时，表白那一行显示「已表白」而不是「该她开口了」。"""
        p = A._v2_panel(_rec(value=100, confess=50, together=True))
        self.assertEqual(p["confessName"], "已表白")
        self.assertEqual([r["name"] for r in p["confessRows"]
                          if r["state"] == "current"], ["已表白"])
        self.assertEqual([r["name"] for r in p["confessRows"]
                          if r["state"] == "passed"][-1], "该她开口了")

    def test_confess_row_stays_open_before_together(self):
        p = A._v2_panel(_rec(value=45, confess=50))
        self.assertEqual(p["confessName"], "该她开口了")

    def test_negative_tiers_never_reach_the_panel(self):
        p = A._v2_panel(_rec(value=26))
        names = [r["name"] for r in p["tierRows"]]
        for bad in ("随时会走", "在等你认错", "爱答不理", "小反感"):
            self.assertNotIn(bad, names)
        self.assertEqual(len(p["tierRows"]), 7)
        self.assertEqual([r["name"] for r in p["tierRows"] if r["state"] == "current"],
                         ["动了心"])
        self.assertEqual([r["gateText"] for r in p["tierRows"]
                          if r["state"] == "current"], ["26"])

    def test_contact_gate_text_has_no_fraction_artifact(self):
        p = A._v2_panel(_rec(contacts=[{"degree": 1, "first": True}]))
        cur = [r for r in p["contactRows"] if r["state"] == "current"][0]
        self.assertEqual(cur["gateText"], "第1档")
        self.assertNotIn("/", cur["gateText"])

    def test_frontend_is_flat_and_default_open(self):
        with open(os.path.join(str(A.BASE_DIR), "web", "persona.html"),
                  encoding="utf-8") as fh:
            html = fh.read()
        # 不能用 .foldbody：它带 max-height + 内部滚动条，观感很差。
        self.assertNotIn('fbody.className = "foldbody"', html)
        self.assertIn('fbody.className = "v2stats"', html)
        self.assertIn("setOpen(open, false)", html)

    def test_frontend_defends_against_legacy_reached_rows(self):
        """旧 5174 还会回一串 reached，前端必须自己兜住，不能等重启。"""
        with open(os.path.join(str(A.BASE_DIR), "web", "persona.html"),
                  encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("if (!isDeg && gate < 0) return;", html)
        self.assertIn('r.state === "passed" ? "passed" : "locked"', html)
        self.assertIn('"第" + gate + "档"', html)

    def test_high_tiers_are_locked_until_confession(self):
        p = A._v2_panel(_rec(value=99))
        high = [r for r in p["tierRows"] if r["gate"] >= 50]
        self.assertEqual(len(high), 2)
        self.assertTrue(all(r["state"] == "locked" for r in high))
        self.assertTrue(all("要先表白" in r["name"] for r in high))
        p2 = A._v2_panel(_rec(value=60, together=True))
        self.assertEqual([r["name"] for r in p2["tierRows"]
                          if r["state"] == "current"], ["在一起了"])

    def test_contact_table_shows_facts_not_value(self):
        p = A._v2_panel(_rec(value=90))
        self.assertEqual(p["contactDegree"], 0)
        self.assertEqual([r["name"] for r in p["contactRows"]
                          if r["state"] == "current"], ["还没碰过"])

    def test_v1_gate_tables_stay_empty_for_v2(self):
        p = A._v2_panel(_rec(value=26))
        self.assertEqual(p["intimacy"], [])
        self.assertEqual(p["tiers"], [])

    def test_frontend_has_fold_and_parenthesized_note(self):
        with open(os.path.join(str(A.BASE_DIR), "web", "persona.html"),
                  encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("（这不是分数解锁，只说明她把话攒到了哪一步）", html)
        self.assertIn("数值与状态条", html)
        self.assertIn("persona_v2_stats_open", html)


class TwoSegmentConfessionTest(unittest.TestCase):
    """女生主动表白：第一段正常回完之后再补一段，且不写成收场。"""

    def test_second_reply_keeps_only_body(self):
        raw = ("她把手攥紧了。「我们在一起吧。」\n"
               "画面：one girl, looking at camera\n"
               "心情：心动 强度8\n状态：校门口 · 校服 · 无 · 傍晚 · 无")
        with mock.patch.object(P, "_llm_call", return_value=raw):
            out = P._confession_reply("head", [], "第一段")
        self.assertIn("我们在一起吧", out)
        self.assertNotIn("画面：", out)
        self.assertNotIn("状态：", out)

    def test_second_reply_failure_is_silent(self):
        with mock.patch.object(P, "_llm_call", side_effect=RuntimeError("boom")):
            self.assertEqual(P._confession_reply("head", [], "第一段"), "")

    def test_task_does_not_ask_for_an_ending(self):
        self.assertIn("不要写成收场", P._AUTO_CONFESS_TASK)
        self.assertIn("接着刚才的场景", P._AUTO_CONFESS_TASK)


class AutoConfessWiringTest(unittest.TestCase):
    """表白值满 50：run_turn 真的会再发一次调用，并把告白接在正文后面。

    2026-11-09 核查用户提问时补的接线测试。`ConfessionTest` 只测结算器
    会不会置 `auto_confess`，不测 persona 有没有真去发第二段；这里把
    `run_turn` 整条路跑一遍（模型调用打桩，不联网）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", root)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(
            P, "_STATE_PATH",
            os.path.join(self.tmp.name, "state", "persona_sessions.json"))
        q.start()
        self.addCleanup(q.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        P.save_cards([{"name": "纱雾", "systemPrompt": "你是纱雾。"}])
        P.switch_line("private_1", P.find_card("纱雾"))
        self.line = P.resolve_line("private_1")

    def _aff(self, **kw):
        base = {"schema": 2, "active": True, "value": 60, "tier": "sure",
                "tierName": "认准你了", "relationDesc": "", "together": False,
                "confess": 50, "confessName": "该她开口了", "confessDesc": "",
                "confessMax": 50, "contacts": [], "contactName": "还没碰过",
                "contactDegree": 0, "yandere": 0, "yandereName": "",
                "arousal": 0, "excite": 0, "corrupt": 0, "submit": 0,
                "auto_confess": True}
        base.update(kw)
        return base

    def _run(self, aff):
        calls = []
        replies = [
            "她把杯子放下，抬眼看你。\n画面：1girl\n心情：紧张 强度5\n"
            "状态：桌边 · 校服 · 无 · 傍晚 · 无",
            "她攥紧了袖口。「我们在一起吧。」"]

        def fake(msgs, **kw):
            calls.append(msgs)
            return replies[min(len(calls) - 1, 1)]

        with mock.patch.object(P, "_judge_v2",
                               return_value=(None, "", {"delta": 0})), \
                mock.patch.object(P.affection, "is_raid", return_value=True), \
                mock.patch.object(P.affection, "is_v2", return_value=True), \
                mock.patch.object(P.affection, "settle", return_value=aff), \
                mock.patch.object(P.affection, "injection_block",
                                  return_value=""), \
                mock.patch.object(P.affection, "dynamic_block",
                                  return_value=""), \
                mock.patch.object(P, "_llm_call", side_effect=fake):
            turn = P.run_turn(self.line, "我有话想跟你说", "private", "")
        return turn, calls

    def test_second_call_fires_and_is_appended(self):
        turn, calls = self._run(self._aff())
        self.assertEqual(len(calls), 2)               # 正文 + 主动表白
        self.assertIn("她把杯子放下", turn.reply)
        self.assertIn("我们在一起吧", turn.reply)
        self.assertEqual(calls[1][-1]["content"], P._AUTO_CONFESS_TASK)

    def test_second_call_sees_the_current_player_line(self):
        """第二段的 messages 必须带玩家**这一轮**说的话，否则她接的是上一轮。"""
        _turn, calls = self._run(self._aff())
        self.assertEqual(calls[1][-2]["role"], "assistant")
        users = [m["content"] for m in calls[1] if m["role"] == "user"]
        self.assertTrue(any("我有话想跟你说" in u for u in users))

    def test_no_second_call_when_the_trigger_is_off(self):
        turn, calls = self._run(self._aff(auto_confess=False))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("我们在一起吧", turn.reply)


class V2PregnancyTest(unittest.TestCase):
    """v2 的怀孕链路（2026-11-09 补）：编剧判 finish、高潮弹窗、孕期注入。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", root)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(
            P, "_STATE_PATH",
            os.path.join(self.tmp.name, "state", "persona_sessions.json"))
        q.start()
        self.addCleanup(q.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        P.save_cards([{"name": "纱雾", "systemPrompt": "你是纱雾。"}])
        P.switch_line("private_1", P.find_card("纱雾"))
        self.line = P.resolve_line("private_1")

    def _aff(self):
        return {"schema": 2, "active": True, "value": 60, "together": True,
                "confess": 50, "yandere": 0, "contacts": [],
                "contactName": "亲过", "contactDegree": 4,
                "auto_confess": False}

    def test_judge_v2_keeps_finish(self):
        raw = '{"intent":"intimate","delta":1,"finish":"inside"}'
        with mock.patch.object(P, "_llm_call", return_value=raw), \
                mock.patch.object(P.affection, "scoring_ctx", return_value={}):
            _i, _n, score = P._judge_v2({"name": "甲"}, "我射在里面了")
        self.assertEqual(score["finish"], "inside")

    def test_v2_orgasm_opens_the_finish_popup(self):
        calls = []

        def fake(msgs, **kw):
            calls.append(msgs)
            return "她软下来。\n画面：1girl\n心情：空 强度1\n状态：床上"

        score = {"delta": 0, "adult": {"orgasm": True}}
        with mock.patch.object(P, "_llm_call", side_effect=fake), \
                mock.patch.object(P.adult_state, "load",
                                  return_value={"orgasm_count": 0}), \
                mock.patch.object(P.adult_state, "apply",
                                  return_value=({"orgasm_count": 1}, [])), \
                mock.patch.object(P.adult_state, "save"), \
                mock.patch.object(P.adult_state, "block", return_value=""), \
                mock.patch.object(P.affection, "is_raid", return_value=True), \
                mock.patch.object(P.affection, "is_v2", return_value=True), \
                mock.patch.object(P.affection, "settle",
                                  return_value=self._aff()), \
                mock.patch.object(P.affection, "injection_block",
                                  return_value=""), \
                mock.patch.object(P.affection, "dynamic_block",
                                  return_value=""), \
                mock.patch.object(P, "_judge_v2",
                                  return_value=(None, "", score)):
            turn = P.run_turn(self.line, "（继续）", "private", "")
        self.assertEqual(turn.aff["climax"]["n"], 1)
        self.assertEqual(turn.aff["climax"]["of"], P.affection.ORGASM_MAX)
        self.assertIsNotNone(P.pending_finish(self.line))

    def test_pregnancy_block_reaches_v2(self):
        calls = []

        def fake(msgs, **kw):
            calls.append(msgs)
            return "她嗯了一声。\n画面：1girl\n心情：静 强度1\n状态：床边"

        with mock.patch.object(P, "_llm_call", side_effect=fake), \
                mock.patch.object(P, "pregnancy_block",
                                  return_value="【孕期】怀孕第 40 天"), \
                mock.patch.object(P.affection, "is_raid", return_value=True), \
                mock.patch.object(P.affection, "is_v2", return_value=True), \
                mock.patch.object(P.affection, "settle",
                                  return_value=self._aff()), \
                mock.patch.object(P.affection, "injection_block",
                                  return_value=""), \
                mock.patch.object(P.affection, "dynamic_block",
                                  return_value=""), \
                mock.patch.object(P, "_judge_v2",
                                  return_value=(None, "", {"delta": 0})):
            P.run_turn(self.line, "在吗", "private", "")
        head = calls[-1][0]["content"]
        self.assertIn("【孕期】怀孕第 40 天", head)


class OldSaveIsolationTest(unittest.TestCase):
    """老档没有 schema 字段 = 永远走 v1，不迁移、不加字段。"""

    def test_v1_blank_has_no_v2_fields(self):
        r = A._blank("", schema=1)
        self.assertNotEqual(r.get("schema"), 2)
        for k in ("contacts", "confess", "yandere", "together", "firsts"):
            self.assertNotIn(k, r)

    def test_v2_record_skips_v1_migration(self):
        r = _rec(value=80)
        before = copy.deepcopy(r)
        self.assertIs(A._migrate(r), r)
        self.assertEqual(r, before)

    def test_old_record_is_not_v2(self):
        r = {"mode": "raid", "value": 42, "level": 4, "tier": "full",
             "excite": 0, "milestone": "", "ach": [], "hist": []}
        self.assertFalse(A._v2_is(r))

    def test_settle_does_not_dispatch_old_record_to_v2(self):
        r = {"mode": "raid", "value": 10, "level": 1, "tier": "touch",
             "arousal": 0, "excite": 0, "scene": False, "orgasms": 0,
             "cross_n": 0, "down": 0, "ending": "", "milestone": "",
             "corrupt": 0, "submit": 0, "suspicion": 0, "hist": [],
             "turns": 0, "day": 0, "streak": 1, "dev": A.dev_blank(),
             "chat": 0, "acts": 0, "idle": 0, "mercy": 3}
        ent = {"affection": r}
        with mock.patch.object(A, "_ensure", return_value=(ent, r)), \
             mock.patch.object(A, "_save_entry"), \
             mock.patch.object(A, "_v2_settle") as v2:
            try:
                A.settle("t_old", {"delta": 1})
            except Exception:                       # noqa: BLE001
                pass                                # 只关心有没有走错分流
            v2.assert_not_called()


class CardStartTest(unittest.TestCase):
    """角色卡的四个 v2 起点：新建这条线时按卡落，没配就和以前一样。

    以前 `_v2_blank_fields()` 硬写 0/0/False，卡面开不了口——想建一张
    「一上来就绷着的病娇卡」或「已经是恋人」的卡做不到（2026-11-09）。
    """

    def _blank(self, card, schema=2):
        with mock.patch.object(P, "active_card", return_value=card):
            return A._blank("t_card_start", schema=schema)

    def test_card_starts_land_on_four_axes(self):
        rec = self._blank({"start_confess": 35, "start_yandere": 60,
                           "start_together": True, "start_value": 60})
        self.assertEqual(rec["confess"], 35)
        self.assertEqual(rec["yandere"], 60)
        self.assertTrue(rec["together"])
        self.assertEqual(rec["value"], 60)
        # 在一起 + 好感 60 → 落档就是「在一起了」，不用先爬
        self.assertEqual(A._v2_tier(rec["value"], rec["together"])["name"],
                         "在一起了")
        # 好感 80 + 在一起 → 一上来就是恋人（以前钳在 50，这档根本填不出）
        hot = self._blank({"start_together": True, "start_value": 80})
        self.assertEqual(A._v2_tier(hot["value"], hot["together"])["name"], "恋人")

    def test_no_card_start_keeps_old_behavior(self):
        rec = self._blank({})
        self.assertEqual((rec["confess"], rec["yandere"], rec["together"]),
                         (0, 0, False))
        self.assertNotIn("adult", rec)

    def test_arousal_tier_seeds_the_body_axis(self):
        """冲动起点在 v2 是身体线的 0~4 档，不是 v1 那个数值。"""
        rec = self._blank({"start_arousal_tier": 3})
        self.assertEqual(rec["adult"]["arousal"], "lead")
        rec0 = self._blank({"start_arousal_tier": 0})
        self.assertNotIn("adult", rec0)          # 0 档 = 不播种子，照旧

    def test_v1_line_ignores_the_v2_start_fields(self):
        rec = self._blank({"start_confess": 35, "start_together": True,
                           "start_arousal_tier": 3}, schema=1)
        self.assertEqual(rec["schema"], 1)
        self.assertNotIn("confess", rec)
        self.assertNotIn("adult", rec)

    def test_together_start_does_not_fire_the_auto_confession(self):
        """已经在一起的卡，不该第一拍又弹一次「她主动表白」。"""
        rec = self._blank({"start_together": True, "start_confess": 50,
                           "start_value": 80})
        res, _ = _settle(rec, {"value_delta": 0, "reason": "闲聊"})
        self.assertFalse(res.get("auto_confess"))


class JudgeV2PrefixCacheTest(unittest.TestCase):
    """2026-11-10：v2 裁判模板「稳定规则在前、这一轮事实在末尾」。

    旧模板第 3 行就是关系档、表白值、游戏内天数这些逐轮变的数值，整段
    提示词每轮都变，DeepSeek 前缀缓存 hit 恒为 0，5,500 token 全价重付。
    现在前半段逐字节不变，只有末尾事实按 miss 付。

    这里守的是**口径**而不是措辞：动态占位符一个都不许留在稳定段里。
    以后往模板里加字段，加错了会立刻红。
    """

    MARK = "## 这一轮的事实"

    def _fill(self, **over):
        """按模板占位符的类型填一份可 format 的 dict。"""
        ph = re.findall(r"%\(([^)]+)\)([sd])", P._SCORE_PROMPT_V2)
        d = {k: (7 if t == "d" else "样本内容") for k, t in ph}
        d.update(over)
        return P._SCORE_PROMPT_V2 % d

    def test_stable_prefix_is_byte_identical_across_turns(self):
        # 稳定段现在还吃进了「她是谁 / 道具清单」：profile 已去掉逐轮 state，
        # 道具清单是常量。变化的是 said / 状态行 / 关系 / 历史 / 账本。
        a = self._fill(said="今天天气不错", relation_name="普通同学",
                       state_line="桌前 · 白衬衫")
        b = self._fill(said="我把她抱起来放到灶台上", relation_name="恋人",
                       state_line="床边 · 睡裙",
                       history="另一段历史", ledger="另一本账",
                       recent="另一条记录", dev="另一组开发度")
        head_a = a.split(self.MARK)[0]
        head_b = b.split(self.MARK)[0]
        self.assertEqual(head_a, head_b)
        # 得够长才跨得过 DeepSeek 64 token 的缓存块粒度
        self.assertGreater(len(head_a), 3000)
        self.assertIn("## 她是谁", head_a)
        self.assertIn("## 道具清单", head_a)
        # 状态没丢，只是挪到了动态段
        self.assertIn("桌前 · 白衬衫", a.split(self.MARK)[1])
        self.assertIn("床边 · 睡裙", b.split(self.MARK)[1])

    def test_no_dynamic_placeholder_in_the_stable_prefix(self):
        """稳定段只留三个恒定输入；其余占位符都得排在末尾动态段。"""
        stable = P._SCORE_PROMPT_V2.split(self.MARK)[0]
        left = set(re.findall(r"%\(([^)]+)\)", stable))
        self.assertTrue(left <= {"facts_guide", "profile", "item_keys"}, left)

    def test_gplan_stays_in_the_dynamic_tail(self):
        """约定事实每轮都变，只能排在末尾 miss 区，不许挪回稳定段。"""
        stable, dynamic = P._SCORE_PROMPT_V2.split(self.MARK, 1)
        self.assertIn("%(gplan)s", dynamic)
        self.assertNotIn("%(gplan)s", stable)

    def test_plan_contract_is_worded_as_still_valid(self):
        stable = P._SCORE_PROMPT_V2.split(self.MARK)[0]
        self.assertIn("到这一拍仍然有效", stable)
        self.assertIn("写空串销账", stable)

    def test_dynamic_values_only_appear_after_the_marker(self):
        text = self._fill(said="一句独一无二的原话",
                          state_line="桌前 · 白衬衫")
        cut = text.index(self.MARK)
        self.assertGreater(text.index("一句独一无二的原话"), cut)
        self.assertGreater(text.index("桌前 · 白衬衫"), cut)
        # 道具清单是常量，挪到 marker 之前吃缓存；玩家这一句仍贴着生成点
        self.assertLess(text.index("## 道具清单"), cut)
        self.assertGreater(text.index("一句独一无二的原话"),
                           text.index("## 道具清单"))

    def test_judge_v2_sends_one_user_message_with_a_stable_head(self):
        """端到端：两轮不同状态/不同原话，发出去的 system 前缀逐字节相同。"""
        replies = [
            '{"intent":"neutral","note":"闲聊","delta":1,'
            '"turn_type":"chat","facts":[],"addressed":true,"secret":false}',
            '{"intent":"intimate","note":"抱她","delta":3,'
            '"turn_type":"action","facts":[],"addressed":true,"secret":false}',
        ]
        ctxs = [{"relation_name": "普通同学", "relation_desc": "还生分",
                 "confess": 3, "confess_max": 50,
                 "recent": "（你们还没什么交集）"},
                {"relation_name": "恋人", "relation_desc": "已经认了",
                 "confess": 31, "confess_max": 50,
                 "recent": "第 6 天，她家厨房，抱过"}]
        seen = []
        with mock.patch.object(P, "_llm_call", side_effect=replies) as m, \
             mock.patch.object(P.affection, "scoring_ctx",
                               side_effect=ctxs), \
             mock.patch.object(P, "_score_profile", return_value="她是谁"):
            for said in ("今天天气不错", "我把她抱起来"):
                P._judge_v2({"name": "甲"}, said, line_key="t_cache")
            seen = [c[0][0][0]["content"] for c in m.call_args_list]
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].split(self.MARK)[0],
                         seen[1].split(self.MARK)[0])
        self.assertIn("我把她抱起来", seen[1])


if __name__ == "__main__":
    unittest.main()
