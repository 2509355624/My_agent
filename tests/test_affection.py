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


_CFG_CACHE = []


def _cfg():
    """测试用的配置：优先读**真实** config 文件，读不到才退默认表。

    原来直接 `deepcopy(DEFAULT_CONFIG)`——但默认表里的 `prompt` 几乎全是
    空串（那是「配置坏了时的兜底」，只有名字没有文案）。于是凡是要断言注入
    文案的测试都会对着空串失败，测的根本不是真实行为。

    读盘只做一次：这个函数在几十个用例里被调用，每次读盘 + deepcopy 会把
    整个文件从 6 秒拖到 60 秒（实测过）。缓存的是**原始 dict**，
    每次返回深拷贝，调用方随便改都不互相污染。
    """
    import copy
    if not _CFG_CACHE:
        try:
            cfg = affection.load_config()
            if isinstance(cfg, dict) and isinstance(cfg.get("tiers"), list):
                _CFG_CACHE.append(cfg)
        except Exception:                                # noqa: BLE001
            pass
    if _CFG_CACHE:
        return copy.deepcopy(_CFG_CACHE[0])
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


class DefaultRaidTest(AffBase):
    """默认攻略模式（2026-11-04 用户要求）。

    以前只有手动切过一次的线才有好感记录，新线全走自由模式——网页右栏整块
    不显示、QQ 尾行没有好感、结算也不跑，玩家进来不知道自己在玩什么。
    """

    def test_new_line_is_raid_by_default(self):
        self._enter()
        key = persona.resolve_line("private_1")
        self.assertTrue(affection.is_raid(key))
        self.assertEqual(affection.status(key)["mode"], "raid")

    def test_default_raid_starts_at_card_start(self):
        """默认开启也要认角色卡的起点，不能一律从 0 开始。"""
        persona.save_cards([_card("妹妹", start_level=2)])
        persona.switch_line("private_1", persona.find_card("妹妹"))
        st = affection.status(persona.resolve_line("private_1"))
        self.assertEqual(st["level"], 2)
        self.assertEqual(st["value"], 20)

    def test_no_card_still_no_record(self):
        """没在演谁就不该凭空建记录（这条线还没选角色）。"""
        self.assertIsNone(affection.status(persona.resolve_line("private_9")))

    def test_card_start_arousal(self):
        """卡上配了开局冲动，新建这条线时就按它起（2026-11-05）。

        痴情师尊那种「冲你已经到顶、只是不想跟你谈感情」的开局，用默认的
        冲动 0 会让模型按「还没起来」演，和卡面自相矛盾。
        """
        persona.save_cards([_card("痴情师尊", start_level=4, start_value=30,
                                  start_arousal=95)])
        persona.switch_line("private_1", persona.find_card("痴情师尊"))
        st = affection.status(persona.resolve_line("private_1"))
        self.assertEqual(st["arousal"], 95)

    def test_card_start_arousal_defaults_to_zero(self):
        persona.save_cards([_card("普通卡")])
        persona.switch_line("private_1", persona.find_card("普通卡"))
        self.assertEqual(affection.status(persona.resolve_line("private_1"))["arousal"], 0)

    def test_switching_to_free_sticks(self):
        """默认攻略 ≠ 切不回自由：封存之后下次读仍然是自由模式。"""
        self._enter()
        key = persona.resolve_line("private_1")
        affection.set_mode(key, False)
        self.assertFalse(affection.is_raid(key))
        self.assertEqual(affection.status(key)["mode"], "free")


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
        # 2026-11-04 晚：上限抬到 100，50 之后补了两档（在一起了 / 交给你了）
        self.assertEqual(affection.tier_for(100, cfg)["key"], "devoted")
        self.assertEqual(affection.tier_for(75, cfg)["key"], "devoted")
        self.assertEqual(affection.tier_for(74, cfg)["key"], "lover")
        self.assertEqual(affection.tier_for(50, cfg)["key"], "lover")
        self.assertEqual(affection.tier_for(40, cfg)["key"], "full")
        self.assertEqual(affection.tier_for(39, cfg)["key"], "care")
        self.assertEqual(affection.tier_for(20, cfg)["key"], "start")
        self.assertEqual(affection.tier_for(19, cfg)["key"], "touch")
        self.assertEqual(affection.tier_for(0, cfg)["key"], "normal")
        self.assertEqual(affection.tier_for(-10, cfg)["key"], "miffed")
        self.assertEqual(affection.tier_for(-50, cfg)["key"], "fallen")

    def test_ending_boundaries(self):
        """2026-11-04 晚：只剩进局子这一条结局。

        好感顶到 max **不再是结局**——用户实测 20 轮就「毕业」，锁死数值之后
        什么都推不动，「这样根本没有意思」。100 也一样，到顶只是到顶。

        2026-11-04 深夜：**下端也不再结局**。用户要在胁迫/半推半就这条线上
        玩下去，报警收场等于玩到一半把桌子掀了。现在 `ending_for` 恒返回
        None，两个端点都只是数字。
        """
        cfg = _cfg()
        self.assertIsNone(affection.ending_for(50, cfg))
        self.assertIsNone(affection.ending_for(100, cfg))
        self.assertIsNone(affection.ending_for(49, cfg))
        self.assertIsNone(affection.ending_for(-49, cfg))
        self.assertIsNone(affection.ending_for(-50, cfg))
        self.assertIsNone(affection.ending_for(-999, cfg))


class SettleTest(AffBase):
    def test_free_mode_is_a_noop(self):
        """切到自由模式之后才不结算（2026-11-04 起默认就是攻略）。"""
        self._enter()
        key = persona.resolve_line("private_1")
        affection.set_mode(key, False)          # 显式封存
        res = affection.settle(key, {"delta": 10, "turn_type": "chat"})
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
        # 2026-11-04 深夜：结局没了，所以「新越线触发结局」这条路径不会再走到。
        # 但**老存档里定格在 jail / graduated 的线还在**，它们每轮都走
        # `settle` 的「结局已锁」提前返回，那条路上的回包照样必须带 mode。
        # 这里直接造一份定格记录来验它。
        ent, rec = affection._ensure(key)
        rec["ending"] = "jail"
        ent["affection"] = rec
        affection._save_entry(key, ent)
        locked = affection.settle(key, {"delta": 3, "turn_type": "chat"})
        self.assertEqual(locked.get("ending"), "jail")
        self.assertEqual(locked.get("mode"), "raid")
        self.assertEqual(locked.get("applied"), 0)
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

    def test_50_is_a_milestone_not_an_ending(self):
        """2026-11-04 晚：好感 50 不再「毕业」锁死，只触发一次里程碑。

        用户原话：「我不需要这个啊，这样根本没有意思了，我 20 轮就达到了」。
        现在 50 给一段叙事（在一起），数值照常往上走，回合照常结算。
        """
        key = self._raid()
        for _ in range(4):
            res = affection.settle(key, {"delta": 13, "turn_type": "action"})
        self.assertEqual(res["just_ended"], "")
        self.assertEqual(res["just_milestone"], "together")
        self.assertEqual(res["value"], 52)
        # 不锁：还能继续涨
        res = affection.settle(key, {"delta": 5, "turn_type": "action"})
        self.assertEqual(res["applied"], 5)
        self.assertEqual(res["value"], 57)
        # 里程碑只报一次，别每轮都「在一起了」
        self.assertEqual(res["just_milestone"], "")

    def test_floor_does_not_end_the_line(self):
        """羁绊跌到下限也不收场（2026-11-04 深夜）。

        用户要在胁迫/半推半就那条线上玩下去，报警收场等于玩到一半掀桌子。
        跌到 -50 之后她可以恨你，但戏还能演——而且还能被哄回来。
        """
        key = self._raid()
        res = {}
        for _ in range(4):
            res = affection.settle(key, {"delta": -13, "turn_type": "action"})
        self.assertEqual(res["value"], affection.MIN_V)
        self.assertEqual(res["just_ended"], "")
        self.assertEqual(res["ending"], "")
        # 还能往回爬
        res = affection.settle(key, {"delta": 8, "turn_type": "action"})
        self.assertEqual(res["applied"], 8)
        self.assertEqual(res["value"], affection.MIN_V + 8)


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
        self.assertEqual(st["value"], 20)        # 二级门槛（2026-11-04 深夜换标尺）

    def test_wipe_numbers_without_line_is_none(self):
        """没在演谁时没东西可清（默认攻略只对有角色线的会话生效）。"""
        self.assertIsNone(affection.wipe_numbers(
            persona.resolve_line("private_9")))

    # ── 越界不再结局（2026-11-04 深夜）──────────────────
    # 用户拍板：想玩胁迫和半推半就，那两条线必然被判 cross=hard；
    # 旧规则「hard 累计 2 次 → 进局子 + 数值锁死」等于在最想玩的那条线上
    # 装了个只响两次的警报。现在越界只有软后果：掉羁绊 + 推高堕落。

    def test_hard_cross_never_ends_the_line(self):
        """连打三次 hard 也不收场，数值照常能推。"""
        key = self._raid()
        for _ in range(3):
            res = affection.settle(key, {"delta": 0, "turn_type": "chat",
                                         "cross": "hard"})
            self.assertEqual(res.get("just_ended"), "")
            self.assertEqual(res.get("ending"), "")
        # 还活着：能接着加分。
        # 2026-11-05 戒备轴上线后要先消戒备：三次 hard 的保底下限（每次 +8）
        # 已把戒备顶到 24 ≥ 20 的折减线，正 delta 会被减半——这正是「越界
        # 有代价」的本意，不卸掉它就不能按原额加分。
        affection.settle(key, {"delta": 0, "turn_type": "chat",
                               "suspicion": -20})
        res = affection.settle(key, {"delta": 10, "turn_type": "action"})
        self.assertEqual(res["applied"], 10)

    def test_hard_cross_raises_corruption(self):
        """越界不会白来——账记在她身上（堕落）。"""
        key = self._raid()
        before = affection.status(key)["corrupt"]
        affection.settle(key, {"delta": 0, "turn_type": "chat",
                               "cross": "hard"})
        after = affection.status(key)["corrupt"]
        self.assertGreater(after, before)

    def test_corruption_does_not_drop_without_soothe(self):
        """堕落只涨不掉：模型没说是安抚，负值一律吞掉。"""
        key = self._raid()
        affection.settle(key, {"delta": 0, "cross": "hard",
                               "corrupt": 15})
        mid = affection.status(key)["corrupt"]
        self.assertGreaterEqual(mid, 15)
        # 给负值但不说这是安抚：不许降
        affection.settle(key, {"delta": 0, "corrupt": -20})
        self.assertEqual(affection.status(key)["corrupt"], mid)

    def test_soothe_is_the_only_way_down(self):
        """显式安抚（soothe=true）才让堕落回落。"""
        key = self._raid()
        affection.settle(key, {"delta": 0, "cross": "hard", "corrupt": 30})
        high = affection.status(key)["corrupt"]
        affection.settle(key, {"delta": 0, "corrupt": -10, "soothe": True})
        self.assertLess(affection.status(key)["corrupt"], high)

    def test_submission_moves_both_ways(self):
        """屈服会涨也会掉——和堕落不是一回事。"""
        key = self._raid()
        affection.settle(key, {"delta": 0, "submit": 20})
        up = affection.status(key)["submit"]
        self.assertEqual(up, 20)
        affection.settle(key, {"delta": 0, "submit": -15})
        self.assertLess(affection.status(key)["submit"], up)

    def test_orgasm_raises_corruption_without_crossing(self):
        """规规矩矩做到高潮也会推高堕落（2026-11-04 深夜，用户追加）。

        没有这一条的话，堕落**只有越界能涨**——玩家要是一路顺着气氛做，
        堕落永远停在 0，两轴四格里只用到左边一列。身体先于心意投降，
        这本身就是一次往下陷。
        """
        key = self._raid()
        for _ in range(5):
            res = affection.settle(key, {"delta": 0, "turn_type": "action",
                                         "in_scene": True, "act": "intense"})
            if res["just_orgasm"]:
                break
        self.assertGreater(res["just_orgasm"], 0)
        self.assertGreater(res["corrupt"], 0)
        self.assertEqual(res["crossN"], 0)      # 一次都没越界

    def test_orgasm_does_not_double_count_on_a_cross(self):
        """越界那轮已经按 hard 记过账了，别再叠一次高潮。"""
        key = self._raid()
        res = affection.settle(key, {"delta": 0, "turn_type": "action",
                                     "in_scene": True, "act": "intense",
                                     "cross": "hard", "allow": "no"})
        if res["just_orgasm"]:
            # hard + allow=no 的floor 是 24，不该变成 24+6
            self.assertEqual(res["corrupt"], 24)

    def test_corruption_clamped_to_range(self):
        key = self._raid()
        for _ in range(5):
            affection.settle(key, {"delta": 0, "corrupt": 40})
        self.assertEqual(affection.status(key)["corrupt"],
                         affection.CORRUPT_MAX)

    def test_tail_line_shows_corruption_once_it_moves(self):
        """堕落 0 不显示（那是压力不是信息），动过之后才挂出来。"""
        res = {"active": True, "level": 2, "levelName": "能抱了",
               "value": 20, "arousal": 0, "corrupt": 0, "submit": 0,
               "ending": ""}
        self.assertNotIn("堕落", affection.tail_line(res))
        res["corrupt"] = 30
        res["corruptName"] = "开始松"
        self.assertIn("堕落 30", affection.tail_line(res))

    def test_injection_carries_corruption_prompt(self):
        """注入里要带堕落档位的正文——那才是越界之后唯一的叙事回报。"""
        res = {"active": True, "value": 20, "level": 2, "levelName": "能抱了",
               "tierName": "开始凿", "arousal": 40, "corrupt": 55,
               "corruptDelta": 12, "submit": 0, "submitDelta": 0,
               "ending": ""}
        # 显式传 cfg：`injection_block` 不传时会去读真实卡库路径，
        # 在测试的临时目录里可能取不到档位文案，断言就变成测环境。
        blk = affection.injection_block(res, _cfg())
        self.assertIn("被你弄成这样", blk)
        self.assertIn("别写成她突然乐意了", blk)


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

    def test_pre_frozen_ending_still_uses_its_prompt(self):
        """老存档定格在结局的线，注入照样要拿结局文案。

        2026-11-04 深夜拿掉结局之后，新线不会再走到这里；但老档里
        `ending="jail"` 的线还在，它们每轮都走这条分支。功能不能因为
        「新线用不上」就悄悄坏掉。
        """
        key = self._raid()
        ent, rec = affection._ensure(key)
        rec["ending"] = "jail"
        ent["affection"] = rec
        affection._save_entry(key, ent)
        res = affection.settle(key, {"delta": 0, "turn_type": "chat"})
        blk = affection.injection_block(res)
        self.assertIn("结局", blk)

    def test_floor_gives_corruption_not_ending(self):
        """跌到下限那一轮的注入：不出现「收场」，出现的是堕落。"""
        key = self._raid()
        res = {}
        for _ in range(4):
            res = affection.settle(key, {"delta": -13, "turn_type": "action"})
        blk = affection.injection_block(res)
        self.assertNotIn("收场", blk)

    def test_tail_line(self):
        res = {"active": True, "value": 22, "tierName": "开始凿",
               "level": 2, "levelName": "能抱了", "ending": "", "applied": 3}
        line = affection.tail_line(res)
        self.assertIn("能抱了", line)
        # 2026-11-04 用户改口要数字：正数**不带加号**，只有负数才带负号。
        # 同日第四次修订：光有分数看不出「还有多远」，改成档内进度。
        # 深夜换标尺后 l2 的门槛是 20、下一档 l3 是 30 —— 22 就是 22/30。
        self.assertIn("亲密 22/30 · 还差 8", line)
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
        affection.set_mode(persona.resolve_line("private_1"), False)
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
        out = persona.handle_command("private_1", "#攻略模式")
        self.assertIn("攻略模式", out)      # 默认就是，回执说「现在就是」
        self.assertTrue(affection.is_raid(persona.resolve_line("private_1")))
        out = persona.handle_command("private_1", "#自由模式")
        self.assertIn("封存", out)
        self.assertFalse(affection.is_raid(persona.resolve_line("private_1")))

    def test_affection_query_and_card(self):
        self._raid()
        self.assertTrue(persona.is_affection_query("#好感"))
        self.assertTrue(persona.is_affection_query("#好感度"))
        self.assertFalse(persona.is_affection_query("好感"))
        self.assertFalse(persona.is_affection_query("好感怎么刷"))
        out = persona.handle_command("private_1", "#好感")
        self.assertIn("攻略档案", out)
        self.assertIn("你们：", out)

    def test_wipe_command_asks_before_deleting(self):
        """「#清空存档」两步（2026-11-04；2026-11-05 加引用闸门）。"""
        key = self._raid()
        affection.settle(key, {"delta": 9, "turn_type": "action"})
        warn = persona.handle_command("private_1", "#清空存档")
        self.assertIn("找不回来", warn)
        self.assertIn(persona.WIPE_CONFIRM, warn)
        self.assertNotEqual(affection.status(key)["value"], 0)   # 这一步什么都不删
        # 模拟 qq_bot 发完警告后回填消息 id
        persona.note_wipe_prompt("private_1", ["w1"])
        out = persona.handle_command("private_1", persona.WIPE_CONFIRM,
                                     quoted_ids=["w1"])
        self.assertIn("存档已删除", out)
        self.assertEqual(affection.status(key)["value"], 0)

    def test_wipe_confirm_without_quoting_the_warning_changes_nothing(self):
        """光回确认句、没引用那条警告：只提示，不删（2026-11-05 用户要求）。"""
        key = self._raid()
        affection.settle(key, {"delta": 9, "turn_type": "action"})
        persona.handle_command("private_1", "#清空存档")
        persona.note_wipe_prompt("private_1", ["w1"])
        out = persona.handle_command("private_1", persona.WIPE_CONFIRM)
        self.assertIn("引用", out)
        self.assertNotEqual(affection.status(key)["value"], 0)
        # 引用了一条不相干的旧消息，同样不算
        out = persona.handle_command("private_1", persona.WIPE_CONFIRM,
                                     quoted_ids=["别的消息"])
        self.assertIn("引用", out)
        self.assertNotEqual(affection.status(key)["value"], 0)

    def test_wipe_confirm_alone_is_plain_text(self):
        """没先发命令，光说确认句要当普通聊天，不能顺手把档删了。"""
        self._raid()
        persona.handle_command("private_1", "#好感")
        self.assertIsNone(persona.handle_command("private_1", persona.WIPE_CONFIRM))

    def test_wipe_pending_cancelled_by_other_message(self):
        """中间说了别的话，上一步的确认就作废——两分钟的窗口不该这么宽。"""
        self._raid()
        persona.handle_command("private_1", "#好感")
        persona.handle_command("private_1", "#清空存档")
        persona.handle_command("private_1", "算了，今天先不聊这个")
        self.assertIsNone(persona.handle_command("private_1", persona.WIPE_CONFIRM,
                                                 quoted_ids=["w1"]))

    def test_wipe_deletes_chat_history(self):
        """真删：聊天历史也要没（老版「重新开始」是不动的）。"""
        key = self._raid()
        persona._save_hist(key, [
            {"role": "user", "content": "上周我们聊了什么"},
            {"role": "assistant", "content": "你说你加班到很晚"},
        ])
        self.assertEqual(len(persona._load_hist(key)), 2)
        persona.handle_command("private_1", "#清空存档")
        persona.note_wipe_prompt("private_1", ["w1"])
        persona.handle_command("private_1", persona.WIPE_CONFIRM, quoted_ids=["w1"])
        self.assertEqual(persona._load_hist(key), [])

    def test_plain_sentence_with_keyword_is_not_a_command(self):
        self._raid()
        self.assertIsNone(persona.handle_command("private_1", "你的好感怎么来的"))
        self.assertIsNone(persona.handle_command("private_1", "我对她有好感"))

    def test_prefixed_clear_no_longer_wipes_without_confirmation(self):
        """2026-11-05：发「#清空」只是删档第一步（亮红灯），不是真删。

        它要引用警告 + 回确认句才动手；不带前缀的「清空」现在连命令都不算。
        """
        key = self._raid()
        affection.settle(key, {"delta": 10, "turn_type": "action"})
        warn = persona.handle_command("private_1", "#清空")
        self.assertIn("清空存档", warn)
        st = affection.status(key)
        self.assertNotEqual(st["value"], 0)     # 这一步什么都没删
        self.assertEqual(st["mode"], "raid")    # 模式保留
        self.assertIsNone(persona.handle_command("private_1", "清空"))

    def test_clear_alias_goes_through_the_same_gate(self):
        """「#重置 / #重开 / #从头开始」也不再直接删——同一个闸门。"""
        key = self._raid()
        affection.settle(key, {"delta": 10, "turn_type": "action"})
        for word in ("#重置", "#重开", "#从头开始"):
            warn = persona.handle_command("private_1", word)
            self.assertIsNotNone(warn, word)
            self.assertIn("清空存档", warn, word)
        self.assertNotEqual(affection.status(key)["value"], 0)


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
        self.assertEqual(st["levelName"], "能抱了")
        # 没给 start_value 就卡在这一级的门槛上，不是 0
        self.assertEqual(st["value"], 20)

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
        """2026-11-05 改：有人在拱气氛的那一轮**不回落**。

        旧规则是「每轮无条件 -15」。打分模型给的常态增量只有 +10~+15
        （情话 +10、独处 +15），净值恒为负 → 夹在 0 上永远不动，
        网页右栏那条冲动条看起来是死的（用户实测反馈）。
        现在只有「这一轮没在拱」（arose <= 0）才回落：有人推就攒得住，
        没人管会凉——「得重新拱气氛」的本意不变。
        """
        key = self._raid_card()
        r1 = affection.settle(key, {"delta": 1, "turn_type": "action", "arousal": 40})
        self.assertEqual(r1["arousal"], 40)          # 0 + 40（有增量就不扣）
        r2 = affection.settle(key, {"delta": 1, "turn_type": "action", "arousal": 40})
        self.assertEqual(r2["arousal"], 80)
        r3 = affection.settle(key, {"delta": 1, "turn_type": "action"})
        self.assertEqual(r3["arousal"], 65)          # 什么都不做就往下掉 15

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

    def test_hard_cross_twice_rises_but_never_ends(self):
        """越界两次：掉分、涨堕落，但**不结局**（2026-11-04 深夜改）。

        旧版第二次 hard 直接进局子。用户要在这条线上玩下去，所以现在
        只有软后果：每次 -20 羁绊，外加堕落涨一截。
        """
        key = self._raid_card()
        r1 = affection.settle(key, {"delta": 0, "turn_type": "chat", "cross": "hard"})
        self.assertEqual(r1["applied"], -affection.CROSS_HARD)
        self.assertEqual(r1["ending"], "")
        co1 = r1["corrupt"]
        r2 = affection.settle(key, {"delta": 0, "turn_type": "chat", "cross": "hard"})
        self.assertEqual(r2["just_ended"], "")
        self.assertEqual(r2["ending"], "")
        self.assertGreater(r2["corrupt"], co1)     # 第二次又碎一点
        self.assertEqual(r2["crossN"], 2)
        # 还能接着玩
        r3 = affection.settle(key, {"delta": 10, "turn_type": "action"})
        self.assertEqual(r3["applied"], 10)

    def test_old_record_gets_level_backfilled(self):
        """加层级之前的老存档不能一夜之间退回「还没碰过」。"""
        old = {"mode": "raid", "value": 22, "tier": "start", "day": "2026-11-01",
               "chat": 0, "acts": 0, "hist": [], "ending": ""}
        rec = affection.record_of({"affection": old})
        # 22 ≥ l2 的门槛 20，老档再补一级 → l3；不补就是「进度被抹」，
        # 老玩家回来发现身体上的关系退回去了（2026-11-04 用户拍板补一级）
        self.assertEqual(rec["level"], 3)
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

    # ── 亲密度进度（2026-11-04 第四次修订）────────────────
    # 用户报「只看到档名叫什么，不知道怎么触发后续剧情」。档名本身会变，
    # 但看不出还差多远；所以补一个「这一档走到下一档」的进度。

    def test_gauge_counts_within_the_current_level(self):
        """分母是**下一档门槛**，不是总上限：l3(30) → l4(40)，34 分差的 6。

        2026-11-04 深夜换标尺：7 级，门槛 0/10/20/30/40/50/65。
        """
        g = affection.intimacy_gauge(3, 34)
        self.assertEqual((g["gate"], g["nextGate"]), (30, 40))
        self.assertEqual(g["need"], 6)
        self.assertEqual(g["pct"], 40)          # (34-30)/10
        self.assertFalse(g["maxed"])
        self.assertEqual(g["label"], "34/40 · 还差 6")

    def test_gauge_says_ready_but_not_risen(self):
        """分数够了、那场戏还没做成 → 「够了 · 差一步」。

        这句话是防玩家把「分够了却不升」当成卡 bug（升一级要 beat + 门槛）。
        不写 `30/30` 也不写 `50/12`：好感能涨到 50，等级却可能卡在 l1，
        分子比分母大那句话就没法看了。
        """
        g = affection.intimacy_gauge(3, 40)     # l3 门槛 30，l4 门槛 40
        self.assertEqual(g["need"], 0)
        self.assertEqual(g["pct"], 100)
        self.assertFalse(g["maxed"])            # 还没升到 l4，别报「到顶」
        self.assertEqual(g["label"], "够了 · 差一步")
        # 溢出也走同一句
        self.assertEqual(affection.intimacy_gauge(1, 50)["label"],
                         "够了 · 差一步")

    def test_gauge_switches_to_tiers_after_max_level(self):
        """等级满级不等于到顶（2026-11-04 晚）：好感上限 100，档位还能往后走。

        7 级满级门槛是 65；可 75（交给你了）还在后面。
        满级后必须改由档位算「下一站」，否则会误报「到顶」，玩家又以为结束了。
        """
        g = affection.intimacy_gauge(6, 70)
        self.assertFalse(g["maxed"])
        self.assertEqual(g["nextGate"], 75)
        self.assertEqual(g["label"], "70/75 · 还差 5")
        # 66 分、等级已满 → 下一站仍是 75
        self.assertEqual(affection.intimacy_gauge(6, 66)["label"],
                         "66/75 · 还差 9")

    def test_gauge_really_maxed_only_after_last_tier(self):
        g = affection.intimacy_gauge(6, 80)
        self.assertTrue(g["maxed"])
        self.assertEqual(g["label"], "80 到顶")
        self.assertEqual(g["pct"], 100)

    def test_gauge_negative_has_no_denominator(self):
        """负分不报「到下一档还差多少」——赎罪期看的是能不能回到 0。"""
        g = affection.intimacy_gauge(2, -22)
        self.assertEqual(g["label"], "-22")
        self.assertEqual(g["pct"], 0)

    def test_gauge_next_name_follows_card(self):
        """下一档叫什么要跟着卡走（同一套叫法，别混进通用名）。"""
        g = affection.intimacy_gauge(
            2, 15, {"level_names": ["邻居", "邻居", "常来串门", "会留你吃饭"]})
        self.assertEqual(g["nextName"], "会留你吃饭")

    def test_gauge_survives_broken_config(self):
        """配置里没有 levels 不能除零崩页面：退回纯数字，pct 夹在 0~100。"""
        from unittest import mock
        with mock.patch.object(affection, "levels_of", return_value=[]):
            g = affection.intimacy_gauge(2, 15)
        self.assertEqual(g["label"], "15")
        self.assertGreaterEqual(g["pct"], 0)
        self.assertLessEqual(g["pct"], 100)


class ExciteTest(AffBase):
    """高潮值（2026-11-04 深夜，原「兴奋度」）：只在进行中累积，场景一停归零。

    和冲动的分工是这一层的关键——冲动值 = 「她此刻想不想做」（每轮回落，
    和羁绊一起决定接不接得住）；高潮值 = 「这一场里她到哪了」（场景内涨、
    场景外没有，决定这一拍怎么写）。

    记法也在这一版改了：**不再是模型给增量**，而是它判「这一轮在干什么」
    （`act`），涨多少由 `orgasm_hints.base` 那张表算。
    """

    def _raid(self):
        persona.save_cards([_card("纱雾")])
        card = persona.find_card("纱雾")
        persona.switch_line("private_1", card)
        key = persona.resolve_line("private_1")
        affection.set_mode(key, True)
        return key

    def _settle(self, key, **score):
        sc = {"delta": 0, "turn_type": "action", "reason": "测试"}
        sc.update(score)
        return affection.settle(key, sc)

    def _base(self, act):
        return affection.act_base(act)[1]

    def test_starts_from_a_baseline_then_accumulates(self):
        """刚进场景不是从 0 起：她已经在这个状态里了。涨多少看动作档位。"""
        key = self._raid()
        res = self._settle(key, in_scene=True, act="foreplay")
        self.assertEqual(res["excite"], affection.EXCITE_START + self._base("foreplay"))
        self.assertTrue(res["in_scene"])
        # 还在里面：继续按动作档累加
        res = self._settle(key, in_scene=True, act="hand")
        self.assertEqual(res["excite"],
                         affection.EXCITE_START + self._base("foreplay")
                         + self._base("hand"))

    def test_action_tier_drives_the_curve(self):
        """同样的轮数，「加重」比「前戏」涨得快——这是动作强度表的意义。"""
        a = self._raid()
        self._settle(a, in_scene=True, act="intense")
        slow = self._raid()
        self.assertEqual(
            self._settle(self._raid(), in_scene=True, act="intense")["excite"]
            > self._settle(slow, in_scene=True, act="foreplay")["excite"], True)

    def test_midway_stop_only_decays(self):
        """中途停下/被打断：高潮值往下走，但不是当场清零。"""
        key = self._raid()
        self._settle(key, in_scene=True, act="intense")
        before = affection.panel(key)["excite"]
        res = self._settle(key, in_scene=False, act="stop")
        self.assertLess(res["excite"], before)
        self.assertGreater(res["excite"], 0, "停一下不该把整场清空")

    def test_one_idle_turn_does_not_reset(self):
        """一轮没有接触只是「这一轮在说别的」：数值缓降，场景还在（2026-11-05）。"""
        key = self._raid()
        self._settle(key, in_scene=True, act="intense")
        before = affection.panel(key)["excite"]
        res = self._settle(key, in_scene=False, act="none")
        self.assertTrue(res["in_scene"], "单轮空闲不算收场")
        self.assertGreater(res["excite"], 0, "中断不清零——这是被投诉的 bug")
        self.assertLess(res["excite"], before)
        self.assertEqual(res["excite"], max(0, before - affection.ORGASM_IDLE_DECAY))

    def test_two_idle_turns_end_the_scene(self):
        """连续两轮没有接触，才算这一场真的结束。"""
        key = self._raid()
        self._settle(key, in_scene=True, act="intense")
        self._settle(key, in_scene=False, act="none")
        res = self._settle(key, in_scene=False, act="none")
        self.assertFalse(res["in_scene"])
        self.assertEqual(res["excite"], 0)
        self.assertEqual(res["orgasms"], 0)

    def test_act_overrides_false_in_scene(self):
        """模型说 in_scene=false 却给了动作：按进行中算（2026-11-05 修）。

        以前只认 in_scene，模型偶尔把这个字段填成 false 或字符串 "false"，
        这一轮就被判成「场景已结束」——高潮值当场清零、网页面板整块收起，
        玩家看到的就是「刚才还有，怎么没了」。
        """
        key = self._raid()
        res = self._settle(key, in_scene=False, act="hand")
        self.assertTrue(res["in_scene"])
        self.assertGreater(res["excite"], 0)
        # 字符串 "false" 也要能读对
        key2 = self._raid()
        res2 = self._settle(key2, in_scene="false", act="foreplay")
        self.assertTrue(res2["in_scene"])

    def test_refusal_with_body_reaction_still_counts(self):
        """她嘴上拒绝、身体确实被碰到：高潮值照涨（2026-11-05 用户要求）。

        情感 AI 要按上下文给分，不能因为「她没答应」就把身体反应判成 0；
        否则被拒的轮次高潮永远上不去，这根轴等于摆设。
        """
        key = self._raid()
        res = self._settle(key, in_scene=False, act="foreplay",
                           allow="no", reason="被碰到但推开")
        self.assertGreater(res["excite"], 0)

    def test_stop_keeps_the_scene_alive_for_one_turn(self):
        """`act=stop` 是「这一轮停了」，不是「这一场结束了」。"""
        key = self._raid()
        self._settle(key, in_scene=True, act="intense")
        res = self._settle(key, in_scene=False, act="stop")
        self.assertTrue(res["in_scene"])
        self.assertGreater(res["excite"], 0)

    def test_model_increment_is_the_main_driver(self):
        """涨多少由这一轮的 AI 判：它给 30 就是 +30，动作表只做兜底（2026-11-05）。"""
        key = self._raid()
        res = self._settle(key, in_scene=True, act="foreplay", excite=30)
        self.assertEqual(res["excite"], affection.EXCITE_START + 30)
        # 没给增量时才回落到动作档位
        res = self._settle(key, in_scene=True, act="hand")
        self.assertEqual(res["excite"],
                         affection.EXCITE_START + 30 + self._base("hand"))

    def test_ai_negative_increment_sets_the_idle_decay(self):
        """空闲轮落多少也听 AI：它给 -5 就只落 5。"""
        key = self._raid()
        self._settle(key, in_scene=True, act="intense")
        before = affection.panel(key)["excite"]
        res = self._settle(key, in_scene=False, act="none", excite=-5)
        self.assertEqual(res["excite"], max(0, before - 5))

    def test_sleep_closes_the_night(self):
        """过夜：场景结束、高潮值与次数清零、冲动回落（2026-11-05）。

        「睡到第二天」以前只推游戏日、不碰好感记录，第二天早上面板还挂着
        昨晚的高潮值，状态行也还是床上的样子。
        """
        key = self._raid()
        self._settle(key, in_scene=True, act="intense", arousal=40)
        self.assertGreater(affection.panel(key)["excite"], 0)
        affection.sleep(key)
        p = affection.panel(key)
        self.assertFalse(p["inScene"])
        self.assertEqual(p["excite"], 0)
        self.assertEqual(p["orgasms"], 0)
        self.assertGreater(p["arousal"], 0)      # 回落不是清零：刚醒来还记得
        self.assertLess(p["arousal"], 40)

    def test_reaches_top_and_falls_back_not_clamped(self):
        """到 100 触发一次高潮并回落到余韵值，不是死死卡在 100。"""
        key = self._raid()
        res = None
        for _ in range(6):
            res = self._settle(key, in_scene=True, act="intense")
        self.assertGreaterEqual(res["orgasms"], 1)
        self.assertLess(res["excite"], affection.EXCITE_MAX)
        self.assertGreater(res["excite"], 0)

    def test_no_cap_however_many_orgasms(self):
        """2026-11-05 取消 5 次封顶：次数只记，到多少次都不强制收场。

        注意：终点不再由次数决定，但**冷却还在**——每两次之间至少隔三轮
        （见 `test_orgasm_cooldown_three_rounds`），所以要走够轮数。
        """
        key = self._raid()
        res = None
        for _ in range(20):
            res = self._settle(key, in_scene=True, act="intense", excite=40)
            self.assertFalse(res["sceneCapped"], "不该再因为次数封顶")
            self.assertTrue(res["in_scene"], "次数到了也还在场里")
        self.assertGreater(res["orgasms"], affection.ORGASM_MAX,
                           "过了 5 次还能继续记")
        self.assertNotIn("coolLeft", res, "冷却期已取消（2026-11-05）")

    def test_orgasm_cooldown_three_rounds(self):
        """高潮之后至少隔三轮才能再高潮（2026-11-05 用户拍板）。

        高潮值是**女主的体力条**，不是男主的蓄力条：她刚被弄到一次，
        不可能两轮之内又到。封顶是逐轮松开的（85 → 95 → 放开），
        所以进度条一直在动，不是死卡在 99。
        """
        key = self._raid()
        marks = []
        for i in range(12):
            r = self._settle(key, in_scene=True, act="intense", excite=40)
            if r["just_orgasm"]:
                marks.append(i)
        self.assertGreaterEqual(len(marks), 3)
        gaps = [b - a for a, b in zip(marks, marks[1:])]
        self.assertTrue(all(g >= 3 for g in gaps),
                        "两次高潮之间至少三轮，实测间隔 %r" % (gaps,))

    def test_cooldown_caps_the_bar(self):
        """冷却期间进度条要**逐轮往上爬**，不是卡死在 99 不动。"""
        key = self._raid()
        seen = []
        for _ in range(6):
            r = self._settle(key, in_scene=True, act="intense", excite=40)
            # 高潮那一轮 `recover` 也被置上了，但它的 cap 还是满值——
            # 那是「刚到」，不是「在冷却」，别算进来。
            if r["recover"] > 0 and not r["just_orgasm"]:
                seen.append((r["excite"], r["exciteCap"]))
        self.assertTrue(seen, "应该有冷却轮")
        caps = [c for _e, c in seen]
        self.assertEqual(caps[:2], sorted(caps[:2]), "封顶要逐轮松开")
        for e, c in seen:
            self.assertLessEqual(e, c)

    def test_scene_ends_by_idle_not_by_count(self):
        """次数不影响收场；连续两轮不接触才算真收场。"""
        key = self._raid()
        for _ in range(10):
            self._settle(key, in_scene=True, act="intense", excite=40)
        # 两轮不接触 → 真收场
        self._settle(key, in_scene=False, act="none")
        res = self._settle(key, in_scene=False, act="none")
        self.assertFalse(res["in_scene"])
        self.assertEqual(res["excite"], 0)
        self.assertEqual(res["orgasms"], 0, "收场后次数归零，下一场从头算")

    def test_tail_line_only_shows_it_in_scene(self):
        """没在场景里不该多一行「高潮 0」——那会让人以为她冷了。"""
        res = {"active": True, "level": 2, "levelName": "能抱了",
               "value": 20, "arousal": 30, "ending": ""}
        self.assertNotIn("高潮", affection.tail_line(res))
        res["excite"] = 62
        # 2026-11-05：取消 5 次封顶，分母没了，只报「第 N 次」
        self.assertIn("高潮 62 · 第 0 次", affection.tail_line(res))

    def test_high_arousal_lets_her_lead(self):
        """冲动够高且接得住时，注入要放她「先动」的许可（用户要的反客为主）。

        2026-11-04 深夜：门槛改成双轴之后，光冲动高不够——羁绊也得够
        （l6 要 65 羁绊 / 88 冲动）。所以这里给的是两样都过的一组数。
        """
        res = {"active": True, "value": 70, "level": 6, "levelName": "要不够",
               "tierName": "在一起", "arousal": 95, "excite": 40, "ending": ""}
        blk = affection.injection_block(res)
        self.assertIn("由她先动", blk)

    def test_high_excite_also_lets_her_lead(self):
        """高潮值够高一样推她主动——不只看冲动。"""
        res = {"active": True, "value": 70, "level": 6, "levelName": "要不够",
               "tierName": "在一起", "arousal": 90, "excite": 90, "ending": ""}
        blk = affection.injection_block(res)
        self.assertIn("由她先动", blk)

    def test_negative_value_never_gets_lead_permission(self):
        """赎罪期不许反客为主：她还在算账，主动靠近不成立。"""
        res = {"active": True, "value": -22, "level": 3, "levelName": "亲过了",
               "tierName": "在等你认错", "arousal": 95, "excite": 90, "ending": ""}
        blk = affection.injection_block(res)
        self.assertNotIn("由她先动", blk)


class CanCrossTest(AffBase):
    """双轴门槛（2026-11-04 深夜）：羁绊和冲动**两条都要过**。

    这是用户实测那个毛病的修复点：旧版只判 `value >= guard - arousal//5`，
    于是「好感 40、冲动 0」也能推倒——中间没有气氛这一环。
    """

    def test_both_axes_required(self):
        cfg = _cfg()
        # l5 做过了：gate 50 / arousal_gate 77
        self.assertFalse(affection._can_cross(60, 5, 0, cfg))
        self.assertFalse(affection._can_cross(40, 5, 90, cfg))
        self.assertTrue(affection._can_cross(60, 5, 80, cfg))

    def test_need_reports_the_gap(self):
        """`_need_to_cross` 报的是**下一级**的门槛，不是当前级。

        站在 lv5「做过了」（50/77），下一级 l6「要不够」要 65/88：
        40 羁绊差 25、50 冲动差 38。
        """
        cfg = _cfg()
        dv, da = affection._need_to_cross(40, 5, 50, cfg)
        self.assertEqual(dv, 25)
        self.assertEqual(da, 38)

    def test_cross_gap_reports_the_current_level(self):
        """`_cross_gap` 报的是**当前级**的门槛：这一轮能不能做。

        同一个位置（lv5、40 羁绊、50 冲动）：差 10 / 差 27。
        两个函数差一整级，混用一次注入就会说错话。
        """
        cfg = _cfg()
        dv, da = affection._cross_gap(40, 5, 50, cfg)
        self.assertEqual(dv, 10)
        self.assertEqual(da, 27)

    def test_need_at_top_level_falls_back_to_current(self):
        """到顶了没有「下一级」，退回按当前级算，不能报一个不存在的门槛。"""
        cfg = _cfg()
        self.assertEqual(affection._need_to_cross(70, affection.LEVEL_MAX, 90, cfg),
                         affection._cross_gap(70, affection.LEVEL_MAX, 90, cfg))

    def test_falls_back_to_old_rule_without_arousal_gate(self):
        """老配置没有 arousal_gate 时退回单轴判法，不能直接崩。"""
        cfg = {"intimacy": {"levels": [
            {"key": "l0", "name": "x", "gate": 0, "guard": 15}]}}
        self.assertTrue(affection._can_cross(20, 0, 0, cfg))
        self.assertFalse(affection._can_cross(0, 0, 0, cfg))


class ArousalViewTest(AffBase):
    """冲动的**展示层**（2026-11-04）：只出词，不出数字。"""

    def test_arousal_name_boundaries(self):
        cfg = _cfg()
        cases = [(0, "没什么"), (19, "没什么"), (20, "有点想"), (44, "有点想"),
                 (45, "想"), (69, "想"), (70, "很想"), (89, "很想"),
                 (90, "忍不住了"), (100, "忍不住了")]
        for v, want in cases:
            self.assertEqual(affection.arousal_name(v, cfg), want, v)

    def test_arousal_falls_back_when_config_missing(self):
        """用户把 arousal_levels 删了也不能空着——回退默认四档。"""
        cfg = _cfg()
        cfg.pop("arousal_levels", None)
        self.assertEqual(affection.arousal_name(80, cfg), "很想")

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
        self.assertEqual(rows[1], "气氛 想")

    def test_tail_line_skips_arousal_on_hard_cross(self):
        """越界那轮气氛不是重点，别把「她退开了」挤掉。"""
        res = {"active": True, "level": 2, "levelName": "能抱了",
               "arousal": 70, "cross": "hard", "ending": ""}
        rows = affection.tail_line(res).splitlines()
        self.assertEqual(rows[0], "❤ 关系 能抱了")
        self.assertEqual(rows[1], "她退开了")

    def test_tail_line_marks_level_up(self):
        res = {"active": True, "level": 3, "levelName": "亲过了",
               "arousal": 50, "rose": 3, "ending": ""}
        rows = affection.tail_line(res).splitlines()
        self.assertEqual(rows[0], "❤ 关系 亲过了 ↑")
        self.assertEqual(rows[1], "气氛 想")

    def test_tail_line_is_one_number_per_line(self):
        """一行一个数值（2026-11-04 用户要求的排版）：亲密 / 冲动。

        关系进度不再是 `距离 3/5`（玩家看不懂 3/5 是在量什么），也不再写
        「第 3 级 / 共 5 级」（级是行话）。第四次修订又把它加回来了，但换成
        「这一档走到下一档」的读法：`亲密 34/40 · 还差 6`——分母就是下一档
        的门槛，玩家一眼能算出还要几分。
        """
        res = {"active": True, "level": 3, "levelMax": 6,
               "levelName": "亲过了", "value": 34, "arousal": 50,
               "ending": ""}
        rows = affection.tail_line(res).splitlines()
        self.assertEqual(rows[0], "❤ 关系 亲过了")
        self.assertEqual(rows[1:], ["气氛 想", "亲密 34/40 · 还差 6", "冲动 50"])
        # 关系和气氛之外的词都是行话，玩家读不懂，一律不出现
        self.assertNotIn("距离", affection.tail_line(res))
        self.assertNotIn("级", affection.tail_line(res))

    def test_tail_line_keeps_minus_sign(self):
        """负数才带负号——掉分的时候玩家要看得见那个减号。

        负分时不报「到下一档还差多少」：赎罪期看的是能不能回到 0，
        那个分母一点意义都没有。
        """
        res = {"active": True, "level": 0, "levelName": "还没碰过",
               "value": -8, "arousal": 0, "ending": ""}
        self.assertIn("亲密 -8", affection.tail_line(res))

    def test_status_exposes_three_axes(self):
        """网页右栏要一次画完三轴，status 必须一次给全。"""
        key = self._raid()
        st = affection.status(key)
        self.assertEqual(st["level"], 0)
        self.assertEqual(st["levelMax"], 6)
        self.assertEqual(st["arousal"], 0)
        self.assertEqual(st["arousalName"], "没什么")
        self.assertIn("crossN", st)


class PanelTest(AffBase):
    """状态面板（2026-11-04 深夜）：把每一档摊平画成「当前值/门槛」。

    用户的原话：「一列下来，每一行它都有对应的一个数值，对应当前的
    一个状态」。所以这里测的是**行数和标记**，不是文案。
    """

    def _raid(self):
        persona.save_cards([_card("纱雾")])
        card = persona.find_card("纱雾")
        persona.switch_line("private_1", card)
        key = persona.resolve_line("private_1")
        affection.set_mode(key, True)
        return key

    def _set(self, key, **kw):
        ent, rec = affection._ensure(key)
        rec.update(kw)
        ent["affection"] = rec
        affection._save_entry(key, ent)
        return rec

    def test_panel_has_all_four_sections(self):
        """四段都要在：身体进度 / 感情 / 冲动 / 高潮。少一段玩家就缺一块信息。"""
        key = self._raid()
        p = affection.panel(key)
        self.assertEqual(len(p["intimacy"]), 7)      # 七级身体进度
        self.assertEqual(len(p["arousals"]), 5)      # 冲动五档
        self.assertEqual(len(p["orgasms_rows"]), 5)  # 高潮五档
        self.assertGreaterEqual(len(p["tiers"]), 5)  # 感情正向档位
        self.assertIn("needValue", p)
        self.assertIn("needArousal", p)

    def test_current_row_is_marked(self):
        """值落在哪一档，那一行就是 current —— 高亮错了整个面板就废了。"""
        key = self._raid()
        # 冲动 52 落在「想」（45~69）
        self._set(key, arousal=52, value=42, level=3)
        p = affection.panel(key)
        cur = [r for r in p["arousals"] if r["state"] == "current"]
        self.assertEqual(len(cur), 1)
        self.assertEqual(cur[0]["name"], "想")

    def test_highlights_follow_the_value_up(self):
        """值往上走，高亮行要跟着往上——这是「看到条被顶满」的全部意义。"""
        key = self._raid()
        seen = []
        for v in (10, 52, 95):
            self._set(key, arousal=v)
            cur = [r["name"] for r in affection.panel(key)["arousals"]
                   if r["state"] == "current"]
            seen.append(cur[0] if cur else "")
        self.assertEqual(seen, ["没什么", "想", "忍不住了"])

    def test_orgasm_section_only_in_scene(self):
        """高潮值没在进行时不该进面板——那不是「0」，是「没有这一项」。"""
        key = self._raid()
        self._set(key, excite=0, scene=False)
        self.assertFalse(affection.panel(key)["inScene"])
        self._set(key, excite=68, scene=True)
        self.assertTrue(affection.panel(key)["inScene"])

    def test_panel_text_lists_every_row(self):
        """「状态」指令的文本版要把每一行都列出来，不能只给一句结论。

        42 分会经 `_migrate` 抬到 lv4「碰过了」（要 40/65），
        下一级是 lv5「做过了」（要 50/77）——所以**两条缺口都在**：
        感情差 8、气氛差 65。这里测的就是「两样都缺时两条都报」。
        """
        key = self._raid()
        self._set(key, value=42, arousal=12)
        t = affection.panel_text(key)
        self.assertIn("身体进度", t)
        self.assertIn("还没碰过", t)
        self.assertIn("要不够", t)
        self.assertIn("感情还差", t)          # 42 < 50
        self.assertIn("气氛还差", t)          # 12 < 77
        # 感情够、只缺气氛：55 分已在 lv5，下一级 lv6 要 65/88，
        # 感情还差 10——所以换 70 分，那才是「只缺气氛」
        self._set(key, value=70, arousal=12)
        t2 = affection.panel_text(key)
        self.assertNotIn("感情还差", t2)      # 70 > 65
        self.assertIn("气氛还差", t2)         # 12 < 88

    def test_panel_brief_reports_only_the_current_rows(self):
        """每轮附着的窄条只报 ▶ 指着的那一格，不铺整张阶梯表（2026-11-05）。"""
        key = self._raid()
        self._set(key, value=42, level=4, arousal=52, corrupt=30, submit=60)
        b = affection.panel_brief(key)
        self.assertIn("此刻", b)
        self.assertIn("身体 ▶ 碰过了", b)
        self.assertIn("冲动 ▶ 想", b)
        self.assertIn("堕落 ▶ 开始松", b)
        self.assertIn("屈服 ▶ 让了", b)
        # 整张表里的其它档不能出现，否则窄条会把卡片正文淹掉
        self.assertNotIn("还没碰过", b)
        self.assertNotIn("要不够", b)
        self.assertNotIn("感情还差", b)

    def test_panel_brief_skips_orgasm_outside_scene(self):
        key = self._raid()
        self._set(key, scene=False)
        self.assertNotIn("高潮", affection.panel_brief(key))
        self._set(key, scene=True, excite=68, orgasms=2)
        b = affection.panel_brief(key)
        self.assertIn("高潮 ▶", b)
        self.assertIn("第 2/5 次", b)

    def test_panel_brief_empty_without_a_line(self):
        """没在演谁就没有窄条（返回空串，调用方别拼）。"""
        self._enter()
        self.assertEqual(affection.panel_brief(
            persona.resolve_line("private_9")), "")

    def test_panel_needs_a_card(self):
        """没在演谁就没有面板（和 detail_nodes 同一口径）。"""
        self._enter()
        self.assertIsNone(affection.panel(persona.resolve_line("private_9")))


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
                         "能碰到了")

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

    def test_milestone_unlocks(self):
        """跨过 50 拿到「在一起」成就（2026-11-04 晚替代原来的「毕业」）。"""
        key = self._raid()
        # 每次 +15（ROUND_CAP），四轮推过 50
        for _ in range(3):
            self._settle(key, delta=15)
        res = self._settle(key, delta=15)
        self.assertEqual(res["just_milestone"], "together")
        self.assertEqual(res["just_ended"], "")
        keys = [a["k"] for a in res.get("ach") or []]
        self.assertIn("together", keys)

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
        self.assertIn("能碰到了", body)
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
        反推出来必然保守，不补就等于把走过的那段路抹了（2026-11-04 用户拍板）。

        2026-11-04 深夜换标尺后重算：门槛 0/10/20/30/40/50/65。
        """
        cases = [(0, 0), (7, 0), (20, 3), (33, 4), (47, 5), (66, 6)]
        for v, want in cases:
            rec = affection._migrate(self._legacy(value=v))
            self.assertEqual(rec["level"], want, v)

    def test_bonus_never_reaches_top_level(self):
        """45 分反推 lv5，补一级就到顶了——顶格是终点站，白送就没有下一步。"""
        rec = affection._migrate(self._legacy(value=45))
        self.assertEqual(rec["level"], affection.LEVEL_MAX - 1)
        # 好感自己够到最高一级门票（65）的，自然就是 6
        rec = affection._migrate(self._legacy(value=66))
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
        """定格在结局的线不会再走结算，结局成就只能在迁移时补。

        2026-11-04 晚：「毕业」退休，老档的 graduated 在迁移时**顺手解锁**
        （数值原样保留，转成里程碑），成就换成 `together`。
        """
        rec = affection._migrate(self._legacy(value=50, ending="graduated"))
        self.assertEqual(rec["ending"], "")          # 解锁了，不再定格
        self.assertEqual(rec["milestone"], "together")
        self.assertIn("together", [a["k"] for a in rec["ach"]])
        rec = affection._migrate(self._legacy(value=-50, ending="jail"))
        self.assertIn("jail", [a["k"] for a in rec["ach"]])

    def test_existing_ach_not_double_filled(self):
        """已经迁移过的记录再读一次，不能把成就重刷一遍。"""
        rec = self._legacy(value=47)
        rec["level"] = 5
        rec["ach"] = [{"k": "lv5", "ts": "11-04 01:00"}]
        out = affection._migrate(rec)
        self.assertEqual([a["k"] for a in out["ach"]], ["lv5"])

    def test_old_graduate_achievement_becomes_together(self):
        """老档 ach 里的 `graduate` 要换成 `together`。

        不换的话成就页会显示「7/12」却只列出 6 个——计数用的是 ach 长度，
        展示用的是 `_keys()`，`graduate` 已经不在里面了。
        """
        rec = self._legacy(value=50)
        rec["ach"] = [{"k": "lv1", "ts": ""}, {"k": "graduate", "ts": ""}]
        out = affection._migrate(rec)
        self.assertEqual([a["k"] for a in out["ach"]], ["lv1", "together"])

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


class SuspicionTest(AffBase):
    """戒备轴（2026-11-05，用户拍板的「爽要带代价」）。

    玩家拍板的两条核心：越界行为在指标没到时要降好感、涨戒备；
    戒备高时好话的收益要打折。判据在打分模型，这里测的是结算器：
    记账、clamp、保底下限、代码层折减、注入的模糊描述。
    """

    def _settle(self, key, **score):
        base = {"delta": 0, "turn_type": "chat"}
        base.update(score)
        return affection.settle(key, base)

    def test_new_line_starts_calm(self):
        key = self._raid()
        res = self._settle(key)
        self.assertEqual(res["suspicion"], 0)
        self.assertEqual(res["suspicionName"], "坦然")

    def test_positive_suspicion_accumulates_and_clamps(self):
        key = self._raid()
        res = self._settle(key, suspicion=20)      # 单轮上限 +20
        self.assertEqual(res["suspicion"], 20)
        self._settle(key, suspicion=20)
        res = self._settle(key, suspicion=20)      # 20+20 clamp 到 40
        self.assertEqual(res["suspicion"], 40)
        self.assertEqual(res["suspicionName"], "戒备")

    def test_negative_suspicion_relieves_but_stays_nonnegative(self):
        key = self._raid()
        self._settle(key, suspicion=15)
        res = self._settle(key, suspicion=-10)
        self.assertEqual(res["suspicion"], 5)
        res = self._settle(key, suspicion=-20)
        self.assertEqual(res["suspicion"], 0)      # 不透支成负数

    def test_hard_cross_has_suspicion_floor(self):
        # 她说了不要还被继续：哪怕模型手松给了 0，戒备至少 +8
        key = self._raid()
        res = self._settle(key, delta=-5, cross="hard", allow="no",
                           suspicion=0)
        self.assertGreaterEqual(res["suspicion"], 8)

    def test_mild_rejected_has_suspicion_floor(self):
        key = self._raid()
        res = self._settle(key, cross="mild", allow="no", suspicion=0)
        self.assertGreaterEqual(res["suspicion"], 4)
        # 她答应了（yes）不设下限——戒备只记模型判的
        res = self._settle(key, cross="mild", allow="yes", suspicion=0)
        self.assertEqual(res["suspicionDelta"], 0)

    def test_high_suspicion_halves_positive_delta(self):
        # 戒备 ≥20 时正 delta 减半：好话的收益打折——「付出代价」的核心
        key = self._raid()
        self._settle(key, suspicion=20)
        res = self._settle(key, delta=10)
        self.assertEqual(res["applied"], 5)
        # 负 delta 不打折：惩罚不因为她在戒备就缩水
        res = self._settle(key, delta=-6)
        self.assertEqual(res["applied"], -6)

    def test_low_suspicion_no_discount(self):
        key = self._raid()
        self._settle(key, suspicion=10)            # 10 < 20：还不到折减线
        res = self._settle(key, delta=8)
        self.assertEqual(res["applied"], 8)

    def test_injection_blurs_the_number(self):
        # 注入只描述态度，不露数字——露了就变成刷警戒循环
        key = self._raid()
        self._settle(key, suspicion=20)
        self._settle(key, suspicion=15)            # 35 → 戒备档
        ent = affection._entry(key)
        rec = affection.record_of(ent)
        res = affection.settle(key, {"delta": 0, "turn_type": "chat"})
        block = affection.injection_block(res)
        self.assertIn("戒备", block)
        self.assertNotIn(str(rec["suspicion"]), block)

    def test_scoring_ctx_carries_suspicion(self):
        # 打分模型要看到她现在起没起疑，才知道这轮放行还是往回打
        key = self._raid()
        ctx = affection.scoring_ctx(key)
        self.assertEqual(ctx["suspicion"], 0)
        self._settle(key, suspicion=18)
        ctx = affection.scoring_ctx(key)
        self.assertEqual(ctx["suspicion"], 18)
        # recent 扩到 8 条：重复衰减要靠它数「同一招用过几次」
        for i in range(10):
            self._settle(key, delta=1)
        ctx = affection.scoring_ctx(key)
        self.assertLessEqual(ctx["recent"].count("·"), 8)


class RollbackTest(AffBase):
    """回退上一轮（2026-11-04）：settle 动手前快照，rollback_last 整体还原。

    settle 动的字段太多（value/tier/arousal/excite/corrupt/submit/suspicion/
    hist…），逐字段倒推漏一个就是脏账——所以实现是整份快照。这里验：
    快照在改动前、还原是全字段、只支持一级。
    """

    def _raid(self):
        persona.save_cards([_card("纱雾")])
        persona.switch_line("private_1", persona.find_card("纱雾"))
        return persona.resolve_line("private_1")

    def test_snapshot_taken_before_mutation(self):
        key = self._raid()
        affection.settle(key, {"delta": 6, "turn_type": "chat",
                               "suspicion": 5, "corrupt": 3})
        prev = affection.record_of(affection._entry(key)).get("_prev")
        self.assertIsInstance(prev, dict)
        self.assertEqual(prev["value"], 0)
        self.assertEqual(prev["suspicion"], 0)
        self.assertEqual(prev["corrupt"], 0)

    def test_rollback_restores_all_axes(self):
        key = self._raid()
        affection.settle(key, {"delta": 6, "turn_type": "chat"})
        affection.settle(key, {"delta": 10, "turn_type": "chat",
                               "suspicion": 12, "corrupt": 8, "submit": 4})
        rec = affection.record_of(affection._entry(key))
        self.assertEqual(rec["value"], 16)
        res = affection.rollback_last(key)
        self.assertTrue(res["rolled_back"])
        self.assertEqual(res["value"], 6)
        rec = affection.record_of(affection._entry(key))
        self.assertEqual(rec["value"], 6)
        self.assertEqual(rec["suspicion"], 0)
        self.assertEqual(rec["corrupt"], 0)
        self.assertEqual(rec["submit"], 0)
        self.assertEqual(rec["hist"], [{"d": 6, "r": "", "ts": rec["hist"][0]["ts"]}])

    def test_rollback_only_one_level(self):
        """退过一次就没有更早的快照了——「回退」不是时光机。"""
        key = self._raid()
        affection.settle(key, {"delta": 6, "turn_type": "chat"})
        affection.settle(key, {"delta": 4, "turn_type": "chat"})
        self.assertIsNotNone(affection.rollback_last(key))
        self.assertIsNone(affection.rollback_last(key))

    def test_free_mode_has_nothing_to_rollback(self):
        key = self._raid()
        affection.set_mode(key, False)
        self.assertIsNone(affection.rollback_last(key))


if __name__ == "__main__":
    unittest.main()