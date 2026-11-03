"""攻略模式（好感度数值层）的测试（2026-11-03）。

覆盖 `app/affection.py` 的结算规则和 persona 集成：

  - 结算：夹上限、纯聊天每日递减、剧情型每日次数、降档滞回、越线结局、
    结局锁定、封存/解冻、重新开始；
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

    def test_chat_diminishing_returns(self):
        """纯聊天递减但**不会归零**：3 2 1 1 之后每 3 轮补 1 分。

        2026-11-03 群友实测：原来第 5 轮起恒为 0，聊 33 轮只涨 6 分，
        玩家以为系统坏了。现在改成「一直聊一直能涨，只是越来越慢」。
        """
        key = self._raid()
        for expect in (3, 2, 1, 1, 1, 0, 0, 1, 0, 0, 1):
            res = affection.settle(key, {"delta": 5, "turn_type": "chat"})
            self.assertEqual(res["applied"], expect)

    def test_one_day_of_chat_can_advance_a_tier(self):
        """聊满一天（30 轮）至少够升一档（10 分），又不能一天冲到高亲密。"""
        key = self._raid()
        for _ in range(30):
            affection.settle(key, {"delta": 5, "turn_type": "chat"})
        value = affection.status(key)["value"]
        self.assertGreaterEqual(value, 10)     # 有进度感：至少升一档
        self.assertLess(value, 20)             # 但不靠纯聊天一天到「开始凿」

    def test_action_bypasses_diminishing_but_has_round_cap(self):
        key = self._raid()
        res = affection.settle(key, {"delta": 99, "turn_type": "action"})
        self.assertEqual(res["applied"], affection.ROUND_CAP)

    def test_action_daily_budget_exhausts_into_chat_track(self):
        """剧情型一天最多 ACTION_DAILY 次大额，超了按聊天递减管。

        用 +4 的小额：5 次共 20 分，别把好感顶到 +50 触发结局锁定。
        """
        key = self._raid()
        for _ in range(affection.ACTION_DAILY):
            res = affection.settle(key, {"delta": 4, "turn_type": "action"})
            self.assertEqual(res["applied"], 4)
        # 第 6 次：走聊天通道。聊天次数还是 0 → 上限 3
        res = affection.settle(key, {"delta": 10, "turn_type": "action"})
        self.assertEqual(res["applied"], 3)

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

    def test_restart_resets_value_keeps_memory(self):
        key = self._raid()
        affection.settle(key, {"delta": 8, "turn_type": "action"})
        out = affection.restart(key)
        self.assertIn("重新攻略", out)
        st = affection.status(key)
        self.assertEqual(st["value"], 0)
        self.assertIsNotNone(persona.active_card(key))  # 角色还在
        self.assertEqual(len(persona._load_hist(key)), 0)  # 没动历史（这里本来就空）

    def test_restart_without_raid_is_none(self):
        self._enter()
        self.assertIsNone(affection.restart(persona.resolve_line("private_1")))


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
        # 2026-11-04：尾行不再暴露数字，玩家只能靠感觉判断进度
        self.assertNotIn("+22", line)

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

    def test_restart_command(self):
        self._raid()
        persona.handle_command("private_1", "好感")     # 生成一条记录
        out = persona.handle_command("private_1", "重新开始")
        self.assertIn("重新攻略", out)

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

    def test_tail_line_hides_numbers(self):
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
        """尾行要常驻气氛：`❤ 关系 · 气氛`。

        用户 2026-11-04 拍板要常驻——多数时候是「平静」，但正因为它一直在，
        突然变成「很乱」才有分量。
        """
        res = {"active": True, "level": 2, "levelName": "说得上话",
               "arousal": 55, "tier": "", "ending": ""}
        self.assertEqual(affection.tail_line(res), "❤ 说得上话 · 很乱")

    def test_tail_line_skips_arousal_on_hard_cross(self):
        """越界那轮气氛不是重点，别把「她退开了」挤掉。"""
        res = {"active": True, "level": 2, "levelName": "说得上话",
               "arousal": 70, "cross": "hard", "ending": ""}
        self.assertEqual(affection.tail_line(res), "❤ 说得上话 · 她退开了")

    def test_tail_line_marks_level_up(self):
        res = {"active": True, "level": 3, "levelName": "说得进心里",
               "arousal": 30, "rose": 3, "ending": ""}
        self.assertEqual(affection.tail_line(res), "❤ 说得进心里 ↑ · 有点乱")

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

    def test_achievement_survives_restart(self):
        """重开清的是数值，不是经历——抹掉勋章玩家下次就不重开了。"""
        key = self._raid()
        res = self._settle(key, delta=20, beat=True)
        self.assertTrue(res.get("ach"))
        before = len(res["ach"])
        affection.restart(key)
        rec = affection.record_of(affection._entry(key))
        self.assertEqual(rec["value"], 0)
        self.assertEqual(len(rec["ach"]), before)

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