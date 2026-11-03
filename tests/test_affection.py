"""攻略模式（好感度数值层）的测试（2026-11-03）。

覆盖 `app/affection.py` 的结算规则和 persona 集成：

  - 结算：夹单轮上限、聊天与剧情同权（无按天额度）、降档滞回、越线结局、
    结局锁定、封存/解冻、清空存档；
  - 集成：攻略模式 run_turn 走打分段 + 档位注入；自由模式完全不变；
    QQ 指令（攻略模式/自由模式/好感/重新开始）；清空重置好感。

配置用 `DEFAULT_CONFIG`（档位文案为空占位），不依赖用户实际编辑的
`config/affection_tiers.json`——用户改文案不该让测试变红。
"""
import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.affection as affection
import app.persona as persona


def _card(name="测试角色", **kw):
    c = {"id": "", "name": name, "description": "", "avatar": "",
         "personality": "", "systemPrompt": "你是%s，说话很轻。" % name,
         "appearancePrompt": "1girl, white hair, red eyes", "outfitPrompt": "",
         "referenceImage": ""}
    c.update(kw)
    return c


def _cfg():
    import copy
    return copy.deepcopy(affection.DEFAULT_CONFIG)


class AffBase(unittest.TestCase):
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
        # 配置钉死成默认档位，和用户的实际配置文件解耦
        c = mock.patch.object(affection, "load_config", _cfg)
        c.start()
        self.addCleanup(c.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        # 「清空存档」的两步确认存在模块级 dict 里，不隔离会串到下一个用例
        persona._WIPE_PENDING.clear()
        self.addCleanup(persona._WIPE_PENDING.clear)

    def _enter(self, key="private_1", name="纱雾"):
        persona.save_cards([_card(name)])
        card = persona.find_card(name)
        persona.switch_line(key, card)
        return card

    def _raid(self, key="private_1", name="纱雾"):
        """进扮演态 + 开攻略模式，返回线键。"""
        self._enter(key, name)
        out = affection.set_mode(persona.resolve_line(key), True)
        self.assertIsNotNone(out)
        return persona.resolve_line(key)


class ConfigTest(AffBase):
    def test_missing_config_falls_back_to_default(self):
        with mock.patch.object(affection, "config_path",
                               return_value=os.path.join(
                                   self.tmp.name, "nope", "x.json")):
            cfg = affection.load_config()
        self.assertEqual([t["key"] for t in cfg["tiers"]],
                         [t["key"] for t in affection.DEFAULT_CONFIG["tiers"]])

    def test_broken_config_falls_back_to_default(self):
        path = os.path.join(self.tmp.name, "broken.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ 坏 json")
        with mock.patch.object(affection, "config_path", return_value=path):
            cfg = affection.load_config()
        self.assertIn("tiers", cfg)

    def test_tier_boundaries(self):
        cfg = _cfg()
        self.assertEqual(affection.tier_for(50, cfg)["key"], "full")
        self.assertEqual(affection.tier_for(40, cfg)["key"], "full")
        self.assertEqual(affection.tier_for(39, cfg)["key"], "care")
        self.assertEqual(affection.tier_for(20, cfg)["key"], "start")
        self.assertEqual(affection.tier_for(19, cfg)["key"], "touch")
        self.assertEqual(affection.tier_for(0, cfg)["key"], "normal")
        self.assertEqual(affection.tier_for(-10, cfg)["key"], "miffed")
        self.assertEqual(affection.tier_for(-50, cfg)["key"], "fallen")

    def test_ending_boundaries(self):
        cfg = _cfg()
        self.assertEqual(affection.ending_for(50, cfg), "graduated")
        self.assertIsNone(affection.ending_for(49, cfg))
        self.assertIsNone(affection.ending_for(-49, cfg))
        self.assertEqual(affection.ending_for(-50, cfg), "jail")


class SettleTest(AffBase):
    def test_free_mode_is_a_noop(self):
        self._enter()
        res = affection.settle(persona.resolve_line("private_1"),
                               {"delta": 10, "turn_type": "chat"})
        self.assertFalse(res["active"])

    def test_result_always_carries_mode(self):
        """回包必须带 mode——网页端拿它渲染开关，缺了会自己跳成自由模式。

        2026-11-03 群友实测：好感涨到 +20 时控件突然显示「已封存」，
        其实数值一动没动，是 `settle` 的返回里根本没这个字段。
        """
        key = self._raid()
        self.assertEqual(
            affection.settle(key, {"delta": 3, "turn_type": "chat"}).get("mode"),
            "raid")
        # 结局锁定那条分支也要带
        res = {}
        for _ in range(8):                     # 单轮夹 ±15，多推几次到顶
            res = affection.settle(key, {"delta": 50, "turn_type": "action"})
            if res.get("ending"):
                break
        locked = affection.settle(key, {"delta": 3, "turn_type": "chat"})
        self.assertTrue(locked.get("ending"))
        self.assertEqual(locked.get("mode"), "raid")
        # 自由模式不结算，但要明确告诉前端「现在是 free」
        affection.set_mode(key, False)
        free = affection.settle(key, {"delta": 3, "turn_type": "chat"})
        self.assertFalse(free["active"])
        self.assertEqual(free.get("mode"), "free")

    def test_chat_no_longer_diminishes(self):
        """纯聊天不再按「当天第几次」打折（2026-11-04 晚用户拍板）。

        原来 3/2/1/1 之后每 3 轮 +1，效果是「今天补不完，明天再来」。
        用户的原话：「没有人喜欢一个限制你 24 小时的游戏」。
        现在每轮按模型给的 delta 全额走，只受单轮上限约束。
        """
        key = self._raid()
        for _ in range(10):
            res = affection.settle(key, {"delta": 3, "turn_type": "chat"})
            self.assertEqual(res["applied"], 3)

    def test_chat_can_keep_advancing_within_one_session(self):
        """一次会话里连着聊就能一直推进，不用等第二天。"""
        key = self._raid()
        for _ in range(20):
            res = affection.settle(key, {"delta": 2, "turn_type": "chat"})
            self.assertEqual(res["applied"], 2)
        self.assertEqual(affection.status(key)["value"], 40)

    def test_action_bypasses_diminishing_but_has_round_cap(self):
        key = self._raid()
        res = affection.settle(key, {"delta": 99, "turn_type": "action"})
        self.assertEqual(res["applied"], affection.ROUND_CAP)

    def test_action_has_no_daily_budget(self):
        """剧情型也不再限制「每天几次大额」：连做 8 轮都是全额。"""
        key = self._raid()
        for _ in range(8):
            res = affection.settle(key, {"delta": 4, "turn_type": "action"})
            self.assertEqual(res["applied"], 4)

    def test_negative_delta_not_diminished(self):
        key = self._raid()
        res = affection.settle(key, {"delta": -8, "turn_type": "chat"})
        self.assertEqual(res["applied"], -8)

    def test_promotion_is_immediate(self):
        key = self._raid()
        # 0（normal）→ +10 应该立刻到 touch
        affection.settle(key, {"delta": 5, "turn_type": "action"})   # +5
        res = affection.settle(key, {"delta": 5, "turn_type": "action"})  # +5 → 10
        self.assertEqual(res["tier"], "touch")
        self.assertTrue(res["changed"])

    def test_demotion_needs_two_confirmations(self):
        """降档滞回：掉出档位区间连续两轮才真降，防阈值边缘反复横跳。"""
        key = self._raid()
        affection.settle(key, {"delta": 12, "turn_type": "action"})  # 12 → touch
        res = affection.settle(key, {"delta": -2, "turn_type": "chat"})  # 10 → 还在档内
        self.assertFalse(res["changed"])
        res = affection.settle(key, {"delta": -3, "turn_type": "chat"})  # 7 < 10
        self.assertEqual(res["value"], 7)
        self.assertEqual(res["tier"], "touch")          # 第一轮：先记着
        self.assertFalse(res["changed"])
        res = affection.settle(key, {"delta": -1, "turn_type": "chat"})  # 6，连续第二轮
        self.assertEqual(res["tier"], "normal")
        self.assertTrue(res["changed"])

    def test_graduated_ends_and_locks(self):
        key = self._raid()
        for _ in range(4):
            res = affection.settle(key, {"delta": 13, "turn_type": "action"})
        self.assertEqual(res["just_ended"], "graduated")
        self.assertEqual(res["value"], affection.MAX_V)
        # 锁定后不再结算
        res = affection.settle(key, {"delta": 5, "turn_type": "action"})
        self.assertEqual(res["applied"], 0)
        self.assertFalse(res["just_ended"])
        self.assertTrue(res["active"])

    def test_jail_ends_and_locks(self):
        key = self._raid()
        for _ in range(4):
            res = affection.settle(key, {"delta": -13, "turn_type": "action"})
        self.assertEqual(res["just_ended"], "jail")
        self.assertEqual(res["value"], affection.MIN_V)


class ModeTest(AffBase):
    def test_mode_commands_need_a_card(self):
        # 没在演谁：开/关都当普通消息不认
        self.assertIsNone(affection.set_mode("private_9", True))
        self.assertIsNone(affection.set_mode("private_9", False))

    def test_free_mode_freezes_and_raid_restores(self):
        key = self._raid()
        affection.settle(key, {"delta": 8, "turn_type": "action"})
        out = affection.set_mode(key, False)
        self.assertIn("封存", out)
        st = affection.status(key)
        self.assertEqual(st["mode"], "free")
        self.assertEqual(st["value"], 8)
        out = affection.set_mode(key, True)
        self.assertIn("恢复", out)
        st = affection.status(key)
        self.assertEqual(st["mode"], "raid")
        self.assertEqual(st["value"], 8)               # 原值接着来

    def test_wipe_numbers_resets_to_card_start(self):
        """清空存档 = 好感整份换新（2026-11-04 用户拍板）。

        和老的 `restart()` 的区别：那个只删数值、留着成就。存档既然真删了，
        「经历过的事」也不该留着——否则删完还能看到「毕业」勋章，很怪。
        """
        key = self._raid()
        affection.settle(key, {"delta": 20, "turn_type": "action", "beat": True})
        self.assertTrue(affection.record_of(affection._entry(key))["ach"])
        fresh = affection.wipe_numbers(key)
        self.assertIsNotNone(fresh)
        rec = affection.record_of(affection._entry(key))
        self.assertEqual(rec["value"], 0)        # 测试卡没配 start_level
        self.assertEqual(rec["ach"], [])         # 成就跟着存档一起没
        self.assertEqual(rec["ending"], "")
        self.assertIsNotNone(persona.active_card(key))   # 角色还在

    def test_wipe_numbers_goes_back_to_card_start(self):
        """配了起点的卡（妹妹这种）删完回到起点，不是掉回陌生人。"""
        persona.save_cards([_card("妹妹", start_level=2)])
        persona.switch_line("private_1", persona.find_card("妹妹"))
        key = persona.resolve_line("private_1")
        affection.set_mode(key, True)
        affection.settle(key, {"delta": 10, "turn_type": "action"})
        affection.wipe_numbers(key)
        st = affection.status(key)
        self.assertEqual(st["level"], 2)
        self.assertEqual(st["value"], 12)        # 二级门槛

    def test_wipe_numbers_without_raid_is_none(self):
        self._enter()
        self.assertIsNone(affection.wipe_numbers(persona.resolve_line("private_1")))

    def _to_jail(self, key):
        """把这条线打到进局子，返回结局 key。

        打到就停：结局之后 `settle` 会走「数值锁定」提前返回，
        再打下去最后一轮拿到的 `just_ended` 反而是 False。
        """
        res = {}
        for _ in range(6):
            res = affection.settle(key, {"delta": -13, "turn_type": "action"})
            if res.get("just_ended") == "jail":
                break
        self.assertEqual(res.get("just_ended"), "jail")
        return key

    def test_free_mode_lifts_ending(self):
        """2026-11-04 用户提：报警结局之后要能切自由模式接着聊。

        结局不是死路——切自由模式就把这一页翻过去：结局标记清掉、数值封存，
        好感**往线内挪一格**（不挪的话切回攻略模式第一轮又拿越线值判一次结局）。
        """
        key = self._to_jail(self._raid())
        out = affection.set_mode(key, False)
        self.assertIn("进局子", out)
        self.assertIn("自由模式", out)
        st = affection.status(key)
        self.assertEqual(st["mode"], "free")
        self.assertEqual(st["ending"], "")
        self.assertEqual(st["value"], affection.MIN_V + 1)

    def test_free_mode_lifts_ending_on_line_already_free(self):
        """老线是 `free` 但结局还挂着的（改动前切过自由模式的那批）。

        这个分支必须排在「已经是自由模式」判断之前，否则玩家永远卡在结局里。
        报警线不一定停在线上（越界次数到顶也会结局），所以 -30 也得挪到 -29。
        """
        key = self._raid()
        ent = affection._entry(key)
        ent["affection"].update({"mode": "free", "value": -30,
                                 "ending": "jail"})
        affection._save_entry(key, ent)
        out = affection.set_mode(key, False)
        self.assertIn("翻过去", out)
        st = affection.status(key)
        self.assertEqual(st["ending"], "")
        self.assertEqual(st["value"], -29)

    def test_mercy_matches_free_mode_toggle(self):
        """「再给我一次机会」和「切自由模式再切回攻略」是同一个效果。

        用户 2026-11-04 明确：-50 变 -49、-30 变 -29，别另搞一套。
        """
        key = self._to_jail(self._raid())
        before = affection.status(key)
        out = affection.mercy(key)
        self.assertIn("她留下了", out)
        st = affection.status(key)
        self.assertEqual(st["ending"], "")
        self.assertEqual(st["value"], affection.MIN_V + 1)
        self.assertEqual(st["mercy"], before["mercy"] - 1)
        self.assertEqual(st["mercyMax"], 3)

    def test_mercy_runs_out_after_three(self):
        """机会只有 3 次，用完就不给了——剩下的路是自由模式 / 重新开始。"""
        key = self._raid()
        for i in range(3):
            self._to_jail(key)
            self.assertIn("还剩 %d 次" % (2 - i), affection.mercy(key))
        self._to_jail(key)
        out = affection.mercy(key)
        self.assertIn("用完", out)
        self.assertEqual(affection.status(key)["ending"], "jail")  # 没被解除

    def test_mercy_needs_an_ending(self):
        """没到结局时用不上，也不扣次数。"""
        key = self._raid()
        out = affection.mercy(key)
        self.assertIn("还没到结局", out)
        self.assertEqual(affection.status(key)["mercy"], 3)

    def test_mercy_command_is_wired(self):
        """QQ 私聊发整句「再给我一次机会」要能进到 affection.mercy。"""
        self._to_jail(self._raid())
        out = persona.handle_command("private_1", "再给我一次机会")
        self.assertIn("她留下了", out)
        self.assertEqual(affection.status(persona.resolve_line("private_1"))
                         ["ending"], "")

    def test_raid_after_lifted_ending_does_not_re_end(self):
        key = self._to_jail(self._raid())
        affection.set_mode(key, False)
        self.assertIn("恢复", affection.set_mode(key, True))
        res = affection.settle(key, {"delta": 3, "turn_type": "chat"})
        self.assertEqual(res["ending"], "")
        self.assertEqual(res["value"], affection.MIN_V + 4)

    def test_raid_on_ended_line_points_at_free_mode(self):
        key = self._to_jail(self._raid())
        out = affection.set_mode(key, True)
        self.assertIn("自由模式", out)
        self.assertEqual(affection.status(key)["ending"], "jail")  # 没说切就不切


class InjectionTest(AffBase):
    def test_block_has_tier_and_delta(self):
        key = self._raid()
        res = affection.settle(key, {"delta": 12, "turn_type": "action",
                                     "reason": "帮她解围"})
        blk = affection.injection_block(res)
        self.assertIn("【肢体接触】", blk)
        self.assertIn("帮她解围", blk)
        # 2026-11-04：注入块不再带好感数字，只给「关系 + 感觉」两件事
        self.assertNotIn("+12", blk)
        self.assertIn("她对你的感觉", blk)

    def test_block_empty_when_free(self):
        self.assertFalse(affection.injection_block({"active": False}))

    def test_just_ended_uses_ending_prompt(self):
        key = self._raid()
        for _ in range(4):
            res = affection.settle(key, {"delta": -13, "turn_type": "action"})
        blk = affection.injection_block(res)
        self.assertIn("进局子", blk)
        self.assertIn("收场", blk)

    def test_tail_line(self):
        res = {"active": True, "value": 22, "tierName": "开始凿",
               "level": 2, "levelName": "说得上话", "ending": "", "applied": 3}
        line = affection.tail_line(res)
        self.assertIn("说得上话", line)
        # 2026-11-04 用户改口要数字：正数**不带加号**，只有负数才带负号
        self.assertIn("好感 22", line)
        self.assertNotIn("+22", line)

    def test_negative_value_never_reads_as_arousal(self):
        """负分时冲动再高也不是「她在犹豫」——不然「赎罪期一路挡掉」的文案
        会和这句打架，模型会演成半推半就。（2026-11-04 用户点出的问题）"""
        key = self._raid()
        for _ in range(2):
            res = affection.settle(key, {"delta": -13, "arousal": 60,
                                         "turn_type": "action"})
        self.assertLess(res["value"], 0)
        blk = affection.injection_block(res)
        self.assertIn("别把这点波动演成愿意", blk)
        self.assertNotIn("她会绕", blk)

    def test_positive_value_keeps_soft_arousal(self):
        key = self._raid()
        res = affection.settle(key, {"delta": 10, "arousal": 40,
                                     "turn_type": "action"})
        blk = affection.injection_block(res)
        self.assertNotIn("别把这点波动演成愿意", blk)

    def test_detail_nodes_needs_record(self):
        self._enter()
        self.assertIsNone(affection.detail_nodes(
            persona.resolve_line("private_1")))
        key = self._raid()
        nodes = affection.detail_nodes(key)
        self.assertEqual(len(nodes), 2)
        self.assertIn("攻略档案", nodes[0])
        self.assertIn("最近的变化", nodes[1])


class RaidTurnTest(AffBase):
    """攻略模式下 run_turn 的集成行为。"""

    KEY = "private_1"
    REPLY = ("好呀。\n画面：1girl, smile\n"
             "心情：开心 强度3\n状态：在家 · 家居服 · 无接触")
    SCORE = ('{"intent": "neutral", "delta": 12, "turn_type": "action",'
             ' "reason": "帮她解围", "note": "叙述了救场"}')

    def test_raid_turn_scores_and_injects(self):
        self._raid()
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=[self.SCORE, self.REPLY]) as m:
            turn = persona.run_turn(self.KEY, "我帮你把小混混赶跑了", "private")
        self.assertEqual(m.call_count, 2)
        # 第二段提示词里要有档位注入块
        head = m.call_args_list[1][0][0][0]["content"]
        self.assertIn("你们现在到这一步了", head)
        self.assertIn("肢体接触", head)
        self.assertIn("帮她解围", head)
        # Turn 带回结算结果
        self.assertIsNotNone(turn.aff)
        self.assertTrue(turn.aff["active"])
        self.assertEqual(turn.aff["applied"], 12)
        self.assertEqual(turn.aff["tierName"], "肢体接触")

    def test_raid_turn_survives_missing_score(self):
        """第一段没交 JSON：按 0 分走，正文照回。"""
        self._raid()
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=["我不是json", self.REPLY]):
            turn = persona.run_turn(self.KEY, "在吗", "private")
        self.assertEqual(turn.reply, "好呀。")
        self.assertIsNotNone(turn.aff)
        self.assertEqual(turn.aff["applied"], 0)

    def test_free_mode_has_no_affection_field(self):
        self._enter()
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=['{"intent": "neutral", "note": "闲聊"}',
                                            self.REPLY]) as m:
            turn = persona.run_turn(self.KEY, "在吗", "private")
        self.assertIsNone(turn.aff)
        head = m.call_args_list[1][0][0][0]["content"]
        self.assertNotIn("你们现在的关系", head)


class ScoreProfileTest(AffBase):
    """打分那段要看得见角色卡的性格和设定（2026-11-03）。

    以前只传名字，所有卡共用一把尺子；现在按角色的感受判分值，难度长在
    卡上。这里钉住三件事：设定真的进了提示词、超长会被截、空卡不炸。
    """

    KEY = "private_1"
    REPLY = ("好呀。\n画面：1girl, smile\n"
             "心情：开心 强度3\n状态：在家 · 家居服 · 无接触")
    SCORE = ('{"intent": "neutral", "delta": 3, "turn_type": "chat",'
             ' "reason": "主动搭话", "note": "闲聊"}')

    def _raid_with(self, **kw):
        persona.save_cards([_card("纱雾", **kw)])
        card = persona.find_card("纱雾")
        persona.switch_line(self.KEY, card)
        self.assertIsNotNone(
            affection.set_mode(persona.resolve_line(self.KEY), True))
        return persona.resolve_line(self.KEY)

    def test_personality_and_description_reach_prompt(self):
        self._raid_with(personality="嘴上凶，怕孤单，被夸就慌",
                        description="画涩漫的独居老师，通宵赶稿")
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=[self.SCORE, self.REPLY]) as m:
            persona.run_turn(self.KEY, "在吗", "private")
        prompt = m.call_args_list[0][0][0][0]["content"]
        self.assertIn("嘴上凶，怕孤单", prompt)
        self.assertIn("画涩漫的独居老师", prompt)
        self.assertIn("她是谁", prompt)
        # 按性格判分的口径也要在
        self.assertIn("先认人，再判分", prompt)

    def test_profile_truncates_long_fields(self):
        p = persona._score_profile({"name": "A",
                                    "personality": "性" * 500,
                                    "description": "设" * 1000})
        self.assertIn("性" * 200, p)
        self.assertNotIn("性" * 201, p)
        self.assertIn("设" * 300, p)
        self.assertNotIn("设" * 301, p)

    def test_profile_survives_empty_card(self):
        self.assertIn("对方", persona._score_profile(None))
        self.assertIn("只留名字", persona._score_profile({"name": "只留名字"}))
        self.assertNotIn("性格", persona._score_profile({"name": "x"}))

    def test_state_goes_with_name(self):
        p = persona._score_profile({"name": "纱雾"}, "在房间")
        self.assertIn("纱雾（在房间）", p)


class CommandTest(AffBase):
    def test_raid_commands_roundtrip(self):
        self._enter()
        out = persona.handle_command("private_1", "攻略模式")
        self.assertIn("攻略模式开启", out)
        self.assertTrue(affection.is_raid(persona.resolve_line("private_1")))
        out = persona.handle_command("private_1", "自由模式")
        self.assertIn("封存", out)
        self.assertFalse(affection.is_raid(persona.resolve_line("private_1")))

    def test_affection_query_and_card(self):
        self._raid()
        self.assertTrue(persona.is_affection_query("好感"))
        self.assertTrue(persona.is_affection_query("#好感度"))
        self.assertFalse(persona.is_affection_query("好感怎么刷"))
        out = persona.handle_command("private_1", "好感")
        self.assertIn("攻略档案", out)
        self.assertIn("你们：", out)

    def test_wipe_command_asks_before_deleting(self):
        """「清空存档」两步（2026-11-04）：先说删什么，回确认句才动手。"""
        key = self._raid()
        affection.settle(key, {"delta": 9, "turn_type": "action"})
        warn = persona.handle_command("private_1", "清空存档")
        self.assertIn("找不回来", warn)
        self.assertIn(persona.WIPE_CONFIRM, warn)
        self.assertNotEqual(affection.status(key)["value"], 0)   # 这一步什么都不删
        out = persona.handle_command("private_1", persona.WIPE_CONFIRM)
        self.assertIn("存档已删除", out)
        self.assertEqual(affection.status(key)["value"], 0)

    def test_wipe_confirm_alone_is_plain_text(self):
        """没先发命令，光说确认句要当普通聊天，不能顺手把档删了。"""
        self._raid()
        persona.handle_command("private_1", "好感")
        self.assertIsNone(persona.handle_command("private_1", persona.WIPE_CONFIRM))

    def test_wipe_pending_cancelled_by_other_message(self):
        """中间说了别的话，上一步的确认就作废——两分钟的窗口不该这么宽。"""
        self._raid()
        persona.handle_command("private_1", "好感")
        persona.handle_command("private_1", "清空存档")
        persona.handle_command("private_1", "算了，今天先不聊这个")
        self.assertIsNone(persona.handle_command("private_1", persona.WIPE_CONFIRM))

    def test_wipe_deletes_chat_history(self):
        """真删：聊天历史也要没（老版「重新开始」是不动的）。"""
        key = self._raid()
        persona._save_hist(key, [
            {"role": "user", "content": "上周我们聊了什么"},
            {"role": "assistant", "content": "你说你加班到很晚"},
        ])
        self.assertEqual(len(persona._load_hist(key)), 2)
        persona.handle_command("private_1", "清空存档")
        persona.handle_command("private_1", persona.WIPE_CONFIRM)
        self.assertEqual(persona._load_hist(key), [])

    def test_plain_sentence_with_keyword_is_not_a_command(self):
        self._raid()
        self.assertIsNone(persona.handle_command("private_1", "你的好感怎么来的"))
        self.assertIsNone(persona.handle_command("private_1", "我对她有好感"))

    def test_clear_resets_affection_keeps_mode(self):
        self._raid()
        affection.settle(persona.resolve_line("private_1"),
                         {"delta": 10, "turn_type": "action"})
        out = persona.handle_command("private_1", "清空")
        self.assertIn("记忆清空", out)
        st = affection.status(persona.resolve_line("private_1"))
        self.assertEqual(st["value"], 0)
        self.assertEqual(st["mode"], "raid")            # 模式保留


class DepthHintTest(unittest.TestCase):
    """场外能翻到多深：档位给事实，写多深交给模型（2026-11-03）。"""

    def test_low_tier_is_shallow(self):
        self.assertIn("不打算让你看到什么", affection.depth_hint("cold"))
        self.assertIn("不打算让你看到什么", affection.depth_hint("fallen"))

    def test_mid_tier_is_partial(self):
        self.assertIn("允许你看到一部分", affection.depth_hint("normal"))

    def test_high_tier_stops_hiding(self):
        self.assertIn("几乎不再藏了", affection.depth_hint("full"))

    def test_unknown_tier_says_nothing(self):
        # 空串而不是兜底文案：多注入一句会被模型当成硬规则
        self.assertEqual(affection.depth_hint(""), "")
        self.assertEqual(affection.depth_hint("no_such_tier"), "")

    def test_injection_block_carries_depth(self):
        # 深度从 2026-11-04 起绑距离层级（不再绑好感档位）
        res = {"active": True, "value": 25, "tier": "start", "level": 4,
               "tierName": "开始凿"}
        blk = affection.injection_block(res, _cfg())
        self.assertIn("让你看到多深", blk)
        self.assertIn("几乎不再藏了", blk)
        # 距离只有 2 级时她不该让玩家翻到私密的东西
        res2 = {"active": True, "value": 25, "tier": "start", "level": 2,
                "tierName": "开始凿"}
        self.assertIn("允许你看到一部分",
                      affection.injection_block(res2, _cfg()))


class LevelTest(AffBase):
    """距离层级 + 冲动 + 越界（2026-11-04）。

    这一层的存在意义是让「她拒绝」有依据：以前只有一根好感轴，好感够了就
    自动解锁一切；现在好感只决定她愿不愿意，能不能往前还要靠一件事撑着。
    """

    def _raid_card(self, **card_kw):
        persona.save_cards([_card("纱雾", **card_kw)])
        card = persona.find_card("纱雾")
        persona.switch_line("private_1", card)
        affection.set_mode(persona.resolve_line("private_1"), True)
        return persona.resolve_line("private_1")

    def test_card_can_start_at_a_level(self):
        """骨科卡这类不该从「陌生人」开局：卡片自己指定起点。"""
        key = self._raid_card(start_level=2)
        st = affection.status(key)
        self.assertEqual(st["level"], 2)
        self.assertEqual(st["levelName"], "说得上话")
        # 没给 start_value 就卡在这一级的门槛上，不是 0
        self.assertEqual(st["value"], 12)

    def test_card_can_start_with_value(self):
        key = self._raid_card(start_level=2, start_value=18)
        self.assertEqual(affection.status(key)["value"], 18)

    def test_card_level_names_override(self):
        key = self._raid_card(start_level=2,
                              level_names=["邻居", "邻居", "常来串门的邻居"])
        self.assertEqual(affection.status(key)["levelName"], "常来串门的邻居")

    def test_level_up_needs_both_event_and_gate(self):
        key = self._raid_card()
        # 只做了事、好感还没到门槛 → 不升
        r1 = affection.settle(key, {"delta": 3, "turn_type": "action", "beat": True})
        self.assertEqual(r1["rose"], 0)
        self.assertEqual(r1["level"], 0)
        # 好感够了、但没做成事 → 还是不升
        r2 = affection.settle(key, {"delta": 6, "turn_type": "action", "beat": False})
        self.assertEqual(r2["rose"], 0)
        self.assertEqual(r2["level"], 0)
        # 两个都满足 → 升一级
        r3 = affection.settle(key, {"delta": 6, "turn_type": "action", "beat": True})
        self.assertEqual(r3["rose"], 1)
        self.assertEqual(r3["level"], 1)

    def test_only_one_level_per_turn(self):
        key = self._raid_card()
        r = affection.settle(key, {"delta": 15, "turn_type": "action", "beat": True})
        self.assertEqual(r["level"], 1)      # 好感 15 够 l2(12)，但一轮只升一级

    def test_level_never_drops(self):
        key = self._raid_card()
        affection.settle(key, {"delta": 15, "turn_type": "action", "beat": True})
        r = affection.settle(key, {"delta": -12, "turn_type": "action"})
        self.assertEqual(r["level"], 1)      # 好感掉回去，距离不退回

    def test_arousal_accumulates_and_decays(self):
        key = self._raid_card()
        r1 = affection.settle(key, {"delta": 1, "turn_type": "action", "arousal": 40})
        self.assertEqual(r1["arousal"], 25)          # 0 + 40 - 15
        r2 = affection.settle(key, {"delta": 1, "turn_type": "action", "arousal": 40})
        self.assertEqual(r2["arousal"], 50)
        r3 = affection.settle(key, {"delta": 1, "turn_type": "action"})
        self.assertEqual(r3["arousal"], 35)          # 什么都不做就往下掉

    def test_arousal_floors_at_zero(self):
        key = self._raid_card()
        r = affection.settle(key, {"delta": 1, "turn_type": "action"})
        self.assertEqual(r["arousal"], 0)

    def test_refused_cross_escalates(self):
        key = self._raid_card()
        r1 = affection.settle(key, {"delta": 0, "turn_type": "chat",
                                    "cross": "mild", "allow": "no"})
        self.assertEqual(r1["applied"], -3)          # 第一次当玩笑，轻扣
        r2 = affection.settle(key, {"delta": 0, "turn_type": "chat",
                                    "cross": "mild", "allow": "no"})
        self.assertEqual(r2["applied"], -6)          # 同一天第二次，加倍

    def test_hesitate_costs_nothing(self):
        key = self._raid_card()
        r = affection.settle(key, {"delta": 0, "turn_type": "chat",
                                   "cross": "mild", "allow": "hesitate"})
        self.assertEqual(r["applied"], 0)            # 半推半就：不罚

    def test_hard_cross_twice_ends_the_line(self):
        key = self._raid_card()
        r1 = affection.settle(key, {"delta": 0, "turn_type": "chat", "cross": "hard"})
        self.assertEqual(r1["applied"], -affection.CROSS_HARD)
        self.assertEqual(r1["ending"], "")
        r2 = affection.settle(key, {"delta": 0, "turn_type": "chat", "cross": "hard"})
        self.assertEqual(r2["just_ended"], affection.ENDING_JAIL)

    def test_old_record_gets_level_backfilled(self):
        """加层级之前的老存档不能一夜之间退回「隔得很远」。"""
        old = {"mode": "raid", "value": 22, "tier": "start", "day": "2026-11-01",
               "chat": 0, "acts": 0, "hist": [], "ending": ""}
        rec = affection.record_of({"affection": old})
        # 22 ≥ l3 的门槛 20，老档再补一级 → l4；不补就是「进度被抹」，
        # 老玩家回来发现身体上的关系退回去了（2026-11-04 用户拍板补一级）
        self.assertEqual(rec["level"], 4)
        self.assertEqual(rec["arousal"], 0)
        self.assertEqual(rec["cross_n"], 0)

    def test_tail_line_has_no_plus_sign(self):
        """正数不带加号（`好感 8`，不是 `好感 +8`）——加号看着像在报奖励。"""
        key = self._raid_card()
        r = affection.settle(key, {"delta": 8, "turn_type": "action"})
        line = affection.tail_line(r)
        self.assertNotIn("+8", line)
        self.assertIn("❤", line)


    def test_score_payload_keeps_new_fields(self):
        """第一段的 JSON 要先被**完整**带出来，settle 才能用。

        曾经在这里重新拼了一个只含 delta/turn_type/reason 的 dict，
        结果模型老老实实判了 cross/beat，全被丢掉，新机制等于没接上。
        """
        self._enter()
        key = persona.resolve_line("private_1")
        raw = ('{"intent": "intimate", "delta": 4, "turn_type": "chat",'
               ' "arousal": 25, "cross": "mild", "allow": "hesitate",'
               ' "beat": true, "reason": "试探", "note": "留她过夜"}')
        with mock.patch.object(persona.llm, "call_llm", return_value=raw):
            intent, _note, score = persona._judge_intent_scored(
                persona.find_card("纱雾"), "今晚别回去了", "", key)
        self.assertEqual(intent, "intimate")
        self.assertEqual(score["arousal"], 25)
        self.assertEqual(score["cross"], "mild")
        self.assertEqual(score["allow"], "hesitate")
        self.assertIs(score["beat"], True)


    def test_tier_copy_switches_with_distance(self):
        """距离没到身体那一步时，前四档只能念感情版本。

        九档原文案里 `touch/start/care/full` 写的是身体进度（「界限已经松了」
        「开始凿」），距离还在「说得上话」就拿出来念，和距离层级直接打架。
        """
        cfg = _cfg()
        full = next(t for t in cfg["tiers"] if t["key"] == "full")
        full["name"], full["prompt"] = "随便凿", "界限已经松了"
        full["name_soft"], full["prompt_soft"] = "彻底信任", "她不再藏着什么"
        self.assertEqual(affection._tier_view(full, 2)["name"], "彻底信任")
        self.assertEqual(affection._tier_view(full, 2)["prompt"], "她不再藏着什么")
        self.assertEqual(affection._tier_view(full, 4)["name"], "随便凿")
        self.assertEqual(affection._tier_view(full, 5)["prompt"], "界限已经松了")

    def test_tier_copy_falls_back_when_no_soft(self):
        cfg = _cfg()
        normal = next(t for t in cfg["tiers"] if t["key"] == "miffed")
        self.assertEqual(affection._tier_view(normal, 0)["name"], "小反感")


class ArousalViewTest(AffBase):
    """冲动的**展示层**（2026-11-04）：只出词，不出数字。"""

    def test_arousal_name_boundaries(self):
        cfg = _cfg()
        cases = [(0, "平静"), (24, "平静"), (25, "有点乱"), (49, "有点乱"),
                 (50, "很乱"), (74, "很乱"), (75, "不设防"), (100, "不设防")]
        for v, want in cases:
            self.assertEqual(affection.arousal_name(v, cfg), want, v)

    def test_arousal_falls_back_when_config_missing(self):
        """用户把 arousal_levels 删了也不能空着——回退默认四档。"""
        cfg = _cfg()
        cfg.pop("arousal_levels", None)
        self.assertEqual(affection.arousal_name(80, cfg), "不设防")

    def test_tail_line_carries_arousal(self):
        """尾行要常驻气氛：首行说关系，第二行单独说气氛。

        用户 2026-11-04 拍板要常驻——多数时候是「平静」，但正因为它一直在，
        突然变成「很乱」才有分量。同日二次修订：**每个词都带标签**，
        原来 `❤ 家里的人 · 平静` 会被读成「她此刻的状态」。
        """
        res = {"active": True, "level": 2, "levelName": "说得上话",
               "arousal": 55, "tier": "", "ending": ""}
        rows = affection.tail_line(res).splitlines()
        self.assertEqual(rows[0], "❤ 关系 说得上话")
        self.assertEqual(rows[1], "气氛 很乱")

    def test_tail_line_skips_arousal_on_hard_cross(self):
        """越界那轮气氛不是重点，别把「她退开了」挤掉。"""
        res = {"active": True, "level": 2, "levelName": "说得上话",
               "arousal": 70, "cross": "hard", "ending": ""}
        rows = affection.tail_line(res).splitlines()
        self.assertEqual(rows[0], "❤ 关系 说得上话")
        self.assertEqual(rows[1], "她退开了")

    def test_tail_line_marks_level_up(self):
        res = {"active": True, "level": 3, "levelName": "说得进心里",
               "arousal": 30, "rose": 3, "ending": ""}
        rows = affection.tail_line(res).splitlines()
        self.assertEqual(rows[0], "❤ 关系 说得进心里 ↑")
        self.assertEqual(rows[1], "气氛 有点乱")

    def test_tail_line_is_one_number_per_line(self):
        """一行一个数值（2026-11-04 用户要求的排版）：好感 / 冲动。

        关系进度不再是 `距离 3/5`（玩家看不懂 3/5 是在量什么），也不再写
        「第 3 级 / 共 5 级」（级是行话）——只留关系叫什么，进度靠叫法自己变。
        """
        res = {"active": True, "level": 3, "levelMax": 5,
               "levelName": "说得进心里", "value": 21, "arousal": 42,
               "ending": ""}
        rows = affection.tail_line(res).splitlines()
        self.assertEqual(rows[0], "❤ 关系 说得进心里")
        self.assertEqual(rows[1:], ["气氛 有点乱", "好感 21", "冲动 42"])
        # 关系和气氛之外的词都是行话，玩家读不懂，一律不出现
        self.assertNotIn("距离", affection.tail_line(res))
        self.assertNotIn("级", affection.tail_line(res))

    def test_tail_line_keeps_minus_sign(self):
        """负数才带负号——掉分的时候玩家要看得见那个减号。"""
        res = {"active": True, "level": 0, "levelName": "隔得很远",
               "value": -8, "arousal": 0, "ending": ""}
        self.assertIn("好感 -8", affection.tail_line(res))

    def test_status_exposes_three_axes(self):
        """网页右栏要一次画完三轴，status 必须一次给全。"""
        key = self._raid()
        st = affection.status(key)
        self.assertEqual(st["level"], 0)
        self.assertEqual(st["levelMax"], 5)
        self.assertEqual(st["arousal"], 0)
        self.assertEqual(st["arousalName"], "平静")
        self.assertIn("crossN", st)


class AchievementTest(AffBase):
    """成就（2026-11-04）：只挂确定性事件，不靠模型判里程碑。"""

    def _settle(self, key, **score):
        sc = {"delta": 0, "turn_type": "action", "reason": "测试"}
        sc.update(score)
        return affection.settle(key, sc)

    def test_unlock_on_level_up(self):
        key = self._raid()
        res = self._settle(key, delta=20, beat=True)
        self.assertEqual(res["level"], 1)
        keys = [a["k"] for a in res["ach"]]
        # 一轮里同时升级和进档是常见的（+20 既到 lv1 又进「肢体接触」），
        # 两个都该解
        self.assertIn("lv1", keys)
        self.assertIn("tier_touch", keys)
        self.assertEqual(next(a for a in res["ach"] if a["k"] == "lv1")["name"],
                         "点头之交")

    def test_card_level_names_drive_achievement_name(self):
        """骨科卡第 1 级叫「妹妹」，成就名也得叫这个——两处不一致像两个系统。"""
        self._enter()
        card = persona.find_card("纱雾")
        card["level_names"] = ["陌生人", "妹妹", "知心", "心动", "相依", "一体"]
        card["start_level"] = 1
        persona.save_cards([card])
        persona.switch_line("private_1", card)
        affection.set_mode(persona.resolve_line("private_1"), True)
        key = persona.resolve_line("private_1")
        res = self._settle(key, delta=10)
        lv1 = next(a for a in res["ach"] if a["k"] == "lv1")
        self.assertEqual(lv1["name"], "妹妹")

    def test_no_duplicate_unlock(self):
        key = self._raid()
        first = self._settle(key, delta=20, beat=True)
        self.assertTrue(first.get("ach"))
        # 第二轮啥也没发生：没升级、没进档，不该再带 ach
        second = self._settle(key, delta=0, turn_type="chat")
        self.assertNotIn("ach", second)

    def test_ending_unlocks(self):
        key = self._raid()
        # 每次 +15（ROUND_CAP），四轮推满 50 越线毕业
        for _ in range(3):
            self._settle(key, delta=15)
        res = self._settle(key, delta=15)
        self.assertEqual(res["just_ended"], "graduated")
        keys = [a["k"] for a in res.get("ach") or []]
        self.assertIn("graduate", keys)

    def test_achievement_dies_with_the_save(self):
        """成就跟着存档走（2026-11-04 翻转）。

        老版本这里叫 `test_achievement_survives_restart`，理由是「抹掉勋章
        玩家就不重开了」。但用户把「重新开始」改成了真删的「清空存档」：
        归档既然整份删，勋章留着就等于没删干净。
        """
        key = self._raid()
        res = self._settle(key, delta=20, beat=True)
        self.assertTrue(res.get("ach"))
        affection.wipe_numbers(key)
        rec = affection.record_of(affection._entry(key))
        self.assertEqual(rec["value"], 0)
        self.assertEqual(rec["ach"], [])

    def test_card_hides_locked_names(self):
        key = self._raid()
        self._settle(key, delta=20, beat=True)
        nodes = affection.achievement_nodes(key)
        self.assertTrue(nodes)
        body = nodes[0]
        self.assertIn("点头之交", body)
        self.assertIn("？？？", body)
        self.assertNotIn("进局子", body)      # 没解锁的不剧透

    def test_no_record_returns_none(self):
        """从没开过攻略：当普通消息，不能凭空回一张空成就表。"""
        self._enter()
        self.assertIsNone(affection.achievement_nodes(
            persona.resolve_line("private_1")))


class MigrationTest(AffBase):
    """老存档升级（2026-11-04）：加了三轴和成就，不能让老玩家一夜回到解放前。"""

    def _legacy(self, value=0, tier="normal", ending=""):
        """造一份 11-04 之前的记录：只有单轴字段，没有 level/arousal/ach。"""
        return {"mode": "raid", "value": value, "tier": tier, "down": 0,
                "ending": ending, "day": affection._today(),
                "chat": 0, "acts": 0,
                "hist": [{"d": 3, "r": "陪她", "ts": "11-03 20:00"}]}

    def test_value_and_tier_untouched(self):
        """数值一个都不能动——迁移只加字段。"""
        rec = affection._migrate(self._legacy(value=47, tier="full"))
        self.assertEqual(rec["value"], 47)
        self.assertEqual(rec["tier"], "full")

    def test_level_backfilled_from_value(self):
        """老档按好感反推层级，再**补一级**——旧系统没有「距离」这个轴，
        反推出来必然保守，不补就等于把走过的那段路抹了（2026-11-04 用户拍板）。"""
        cases = [(0, 0), (7, 2), (20, 4), (33, 4), (47, 5)]
        for v, want in cases:
            rec = affection._migrate(self._legacy(value=v))
            self.assertEqual(rec["level"], want, v)

    def test_bonus_never_reaches_top_level(self):
        """33 分反推 lv4，补一级就到顶了——顶格是终点站，白送就没有下一步。"""
        rec = affection._migrate(self._legacy(value=33))
        self.assertEqual(rec["level"], affection.LEVEL_MAX - 1)
        # 好感自己够到最高一级门票（40）的，自然就是 5
        rec = affection._migrate(self._legacy(value=41))
        self.assertEqual(rec["level"], affection.LEVEL_MAX)

    def test_zero_progress_gets_no_bonus(self):
        """一分没有的老档不补：那是真的还没开始，补一级等于白送。"""
        rec = affection._migrate(self._legacy(value=0))
        self.assertEqual(rec["level"], 0)
        self.assertEqual(rec["ach"], [])

    def test_level_achievements_granted(self):
        """层级只升不降，反推出第 N 级 = 前 N 级都走过，可以确定地补。"""
        rec = affection._migrate(self._legacy(value=47))
        got = [a["k"] for a in rec["ach"]]
        self.assertEqual(got, ["lv1", "lv2", "lv3", "lv4", "lv5"])

    def test_tier_achievements_not_granted(self):
        """档位会涨会掉，确认不了当年是否首次进过，所以不补。"""
        rec = affection._migrate(self._legacy(value=47, tier="full"))
        self.assertFalse([a for a in rec["ach"] if a["k"].startswith("tier_")])

    def test_ending_achievement_granted(self):
        """定格在结局的线不会再走结算，结局成就只能在迁移时补。"""
        rec = affection._migrate(self._legacy(value=50, ending="graduated"))
        self.assertIn("graduate", [a["k"] for a in rec["ach"]])
        rec = affection._migrate(self._legacy(value=-50, ending="jail"))
        self.assertIn("jail", [a["k"] for a in rec["ach"]])

    def test_existing_ach_not_double_filled(self):
        """已经迁移过的记录再读一次，不能把成就重刷一遍。"""
        rec = self._legacy(value=47)
        rec["level"] = 5
        rec["ach"] = [{"k": "lv5", "ts": "11-04 01:00"}]
        out = affection._migrate(rec)
        self.assertEqual([a["k"] for a in out["ach"]], ["lv5"])

    def test_migration_persists_once(self):
        """迁移要落盘一次：不然用户改一次 gate，同一条线的层级就跟着漂。"""
        key = self._raid()
        ent = persona._entry(key)
        ent["affection"] = self._legacy(value=33, tier="care")
        affection._save_entry(key, ent)
        # 读一次：触发迁移并写回（33 分反推第 3 级，老档再补一级到第 4 级）
        got = affection.record_of(affection._entry(key))
        self.assertEqual(got["level"], 4)
        self.assertEqual(got["value"], 33)
        # 直接看磁盘：level / ach 已经落进去了
        import json
        with open(persona._STATE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        on_disk = raw[key]["affection"]
        self.assertIn("level", on_disk)
        self.assertEqual(on_disk["level"], 4)
        self.assertIn("ach", on_disk)


if __name__ == "__main__":
    unittest.main()