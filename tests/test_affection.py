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

    def test_chat_diminishing_returns(self):
        """纯聊天当天递减：+3 → +2 → +1 → +1 → 0。防甜言蜜语刷满。"""
        key = self._raid()
        for expect in (3, 2, 1, 1, 0, 0):
            res = affection.settle(key, {"delta": 5, "turn_type": "chat"})
            self.assertEqual(res["applied"], expect)

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
        self.assertIn("归零", out)
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
        self.assertIn("+12", blk)

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
               "ending": "", "applied": 3}
        self.assertIn("开始凿", affection.tail_line(res))
        self.assertIn("+22", affection.tail_line(res))

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
        self.assertIn("你们现在的关系", head)
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
        self.assertIn("当前好感", out)

    def test_restart_command(self):
        self._raid()
        persona.handle_command("private_1", "好感")     # 生成一条记录
        out = persona.handle_command("private_1", "重新开始")
        self.assertIn("归零", out)

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


if __name__ == "__main__":
    unittest.main()
