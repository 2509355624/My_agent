# -*- coding: utf-8 -*-
"""主动接话判断与「没点名也要不要开口」链路的测试。

判断器换过一次（本地 Laya 判别式模型 → LLM，见 app/interject.py 开头的实测
结论），这里固住的是**接口和闸门**：模式开关、试点群、节流、冷却、降级，
以及 qq_bot 侧「不 @ 的消息怎么走」。
"""
import json
import time
import unittest
from unittest import mock

from app import agents as agent_store
from app import interject
from app import qq_bot


def _group_event(text="大家好", group_id=1041079621, user_id=111, self_id=999):
    return json.dumps({
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": user_id,
        "self_id": self_id,
        "sender": {"nickname": "张三"},
        "message": [{"type": "text", "data": {"text": text}}],
    })


class _StateIsolationMixin:
    """隔离模块级状态：冷却 / 节流字典 + 真实 settings.json。

    冷却 / 节流是模块级字典，用例之间必须清干净，否则互相串；
    load_settings 读的是 agents/qq/settings.json（用户在管理页点过静音），
    走真实 decide 链路的用例必须挡住，否则用户拨的开关会让用例集体翻车
    ——跟 _log_verdict 漏挡是同一类污染。
    """

    def setUp(self):
        self._clear()
        self.addCleanup(self._clear)
        p = mock.patch.object(interject.agent_store, "load_settings",
                              return_value={})
        p.start()
        self.addCleanup(p.stop)
        # 概率门带随机，判断间隔默认 15 秒——两者都会让其它用例变得时灵时不
        # 灵。这里统一钉成「每次都判」，要看门本身的行为去 ChanceGateTest /
        # GapFromSettingsTest。
        p = mock.patch.object(interject.agent_store, "interject_chance",
                              return_value=100)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(interject.agent_store, "interject_min_gap",
                              return_value=0)
        p.start()
        self.addCleanup(p.stop)
        # _run_turn 开头会做表情包收藏（网络下载）——一律挡掉；
        # 清单注入同理，别让它读真实库存
        p = mock.patch.object(qq_bot.stickers, "collect", return_value=0)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(qq_bot.stickers, "catalog", return_value="")
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _clear():
        with interject._state_lock:
            interject._last_spoke.clear()
            interject._last_judged.clear()


class ModeTest(unittest.TestCase):
    """三种模式的开关语义：off 什么都不做，shadow 判断但不发言。"""

    def _mode(self, value):
        p = mock.patch.object(interject, "QQ_INTERJECT_MODE", value)
        p.start()
        self.addCleanup(p.stop)

    def test_off_is_fully_inert(self):
        self._mode("off")
        self.assertFalse(interject.enabled())
        self.assertFalse(interject.speaking())

    def test_shadow_judges_but_never_speaks(self):
        self._mode("shadow")
        self.assertTrue(interject.enabled())
        self.assertFalse(interject.speaking())

    def test_on_speaks(self):
        self._mode("on")
        self.assertTrue(interject.enabled())
        self.assertTrue(interject.speaking())

    def test_unknown_mode_falls_back_to_off(self):
        self._mode("yes")
        self.assertFalse(interject.enabled())
        self.assertFalse(interject.speaking())


class ParseTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(interject._parse("接"), "接")
        self.assertEqual(interject._parse("不接"), "不接")

    def test_no_means_the_more_specific_token_wins(self):
        # 先看「不接」：否则「不接」会被「接」误命中
        self.assertEqual(interject._parse("不接。"), "不接")
        self.assertEqual(interject._parse("我觉得不接比较好"), "不接")

    def test_with_leading_noise(self):
        self.assertEqual(interject._parse("好的，接"), "接")

    def test_unrecognised_is_empty(self):
        # 认不出来当「不接」处理（返回空串，由调用方转成 False）
        self.assertEqual(interject._parse("抱歉我不知道"), "")
        self.assertEqual(interject._parse(""), "")
        self.assertEqual(interject._parse(None), "")

    def test_only_looks_at_head(self):
        # 长回复里出现「接」不算数，避免模型絮叨时被误判
        self.assertEqual(
            interject._parse("这个问题嘛需要仔细分析一下然后我再看要不要接着说话"),
            "")


class DecideGuardTest(_StateIsolationMixin, unittest.TestCase):
    """不该判断的时候必须一个调用都不发出去。"""

    def setUp(self):
        super().setUp()
        # test_empty_group_list_means_all_groups 走的是真实判断链路，必须挡住
        # 落盘——否则会把 mock 数据写进 agents/qq/interject/ 的真实日志里
        p = mock.patch.object(interject, "_log_verdict")
        p.start()
        self.addCleanup(p.stop)

    def _mode(self, value):
        p = mock.patch.object(interject, "QQ_INTERJECT_MODE", value)
        p.start()
        self.addCleanup(p.stop)

    def test_off_returns_none(self):
        self._mode("off")
        with mock.patch.object(interject, "call_llm") as llm:
            self.assertIsNone(interject.decide("qq", "1041079621"))
        llm.assert_not_called()

    def test_group_not_in_pilot_list(self):
        self._mode("shadow")
        p = mock.patch.object(interject, "QQ_INTERJECT_GROUPS", ["999"])
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(interject, "call_llm") as llm:
            self.assertIsNone(interject.decide("qq", "1041079621"))
        llm.assert_not_called()

    def test_muted_group_returns_none(self):
        # 管理页把群静音后，settings.json 热生效——判断一个都不发
        self._mode("shadow")
        p = mock.patch.object(interject.agent_store, "load_settings",
                              return_value={"interject_muted":
                                            ["1041079621"]})
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(interject, "call_llm") as llm:
            self.assertIsNone(interject.decide("qq", "1041079621"))
        llm.assert_not_called()

    def test_mute_is_per_group(self):
        self._mode("shadow")
        p = mock.patch.object(interject, "QQ_INTERJECT_GROUPS", [])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(interject.agent_store, "load_settings",
                              return_value={"interject_muted": ["999"]})
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(interject.recent, "format_recent",
                               return_value="张三：在吗"), \
                mock.patch.object(interject, "call_llm",
                                  return_value="不接") as llm:
            verdict = interject.decide("qq", "1041079621")
        self.assertIsNotNone(verdict)
        llm.assert_called_once()

    def test_empty_group_list_means_all_groups(self):
        self._mode("shadow")
        p = mock.patch.object(interject, "QQ_INTERJECT_GROUPS", [])
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(interject.recent, "format_recent",
                               return_value="张三：在吗"), \
                mock.patch.object(interject, "call_llm", return_value="接"):
            self.assertIsNotNone(interject.decide("qq", "1041079621"))

    def test_no_context_returns_none(self):
        self._mode("shadow")
        with mock.patch.object(interject.recent, "format_recent",
                               return_value=""), \
                mock.patch.object(interject, "call_llm") as llm:
            self.assertIsNone(interject.decide("qq", "1041079621"))
        llm.assert_not_called()

    def test_throttled_returns_none(self):
        self._mode("shadow")
        p = mock.patch.object(interject, "QQ_INTERJECT_GROUPS", [])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(interject.agent_store, "interject_min_gap",
                              return_value=600)
        p.start()
        self.addCleanup(p.stop)
        interject._mark_judged("qq", "1041079621")     # 刚判过
        with mock.patch.object(interject.recent, "format_recent",
                               return_value="张三：在吗"), \
                mock.patch.object(interject, "call_llm") as llm:
            self.assertIsNone(interject.decide("qq", "1041079621"))
        llm.assert_not_called()

    def test_on_mode_skips_judging_entirely_during_cooldown(self):
        # 正式模式下冷却中连判断都不做——判完反正开不了口，白花一次调用。
        # 影子模式不在此列（观察期要照判），由 DecideVerdictTest 覆盖。
        self._mode("on")
        p = mock.patch.object(interject, "QQ_INTERJECT_GROUPS", [])
        p.start()
        self.addCleanup(p.stop)
        interject.mark_spoke("qq", "1041079621")       # 刚开口过
        with mock.patch.object(interject.recent, "format_recent",
                               return_value="张三：在吗"), \
                mock.patch.object(interject, "call_llm") as llm:
            self.assertIsNone(interject.decide("qq", "1041079621"))
        llm.assert_not_called()


class DecideVerdictTest(_StateIsolationMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for name, value in (("QQ_INTERJECT_MODE", "shadow"),
                            ("QQ_INTERJECT_GROUPS", [])):
            p = mock.patch.object(interject, name, value)
            p.start()
            self.addCleanup(p.stop)
        # 冷却秒数现在挂在 agent_store（settings 层），管理页可调
        p = mock.patch.object(interject.agent_store, "interject_cooldown",
                              return_value=180)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(interject.recent, "format_recent",
                              return_value="张三：在吗")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(interject.agent_store, "agent_config",
                              return_value={"provider": "deepseek",
                                            "model": "deepseek-flash"})
        p.start()
        self.addCleanup(p.stop)
        # 这些用例走到的是真实判断链路，必须挡住落盘——否则会把 mock 数据
        # 写进 agents/qq/interject/ 的真实日志里，污染用户的数据
        p = mock.patch.object(interject, "_log_verdict")
        p.start()
        self.addCleanup(p.stop)

    def _llm(self, out):
        p = mock.patch.object(interject, "call_llm", return_value=out)
        p.start()
        self.addCleanup(p.stop)

    def test_take_passes_when_cooled(self):
        self._llm("接")
        v = interject.decide("qq", "1041079621")
        self.assertEqual(v["choice"], "接")
        self.assertTrue(v["want"])
        self.assertTrue(v["cooled"])
        self.assertTrue(v["pass"])

    def test_take_blocked_by_cooldown(self):
        self._llm("接")
        interject.mark_spoke("qq", "1041079621")      # 刚开口过
        v = interject.decide("qq", "1041079621")
        self.assertTrue(v["want"])
        self.assertFalse(v["cooled"])
        self.assertFalse(v["pass"])

    def test_decline(self):
        self._llm("不接")
        v = interject.decide("qq", "1041079621")
        self.assertFalse(v["want"])
        self.assertFalse(v["pass"])

    def test_llm_failure_degrades_to_no(self):
        p = mock.patch.object(interject, "call_llm",
                              side_effect=RuntimeError("超时"))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(interject.log, "exception")
        p.start()
        self.addCleanup(p.stop)
        self.assertIsNone(interject.decide("qq", "1041079621"))

    def test_provider_and_model_are_passed_explicitly(self):
        # 后台隐形调用必须显式传 provider/model，否则会静默回退全局默认
        # （跟 memory.py 的摘要漏传是同一个坑）
        p = mock.patch.object(interject, "call_llm", return_value="不接")
        llm = p.start()
        self.addCleanup(p.stop)
        interject.decide("qq", "1041079621")
        _, kwargs = llm.call_args
        self.assertEqual(kwargs.get("provider"), "deepseek")
        self.assertEqual(kwargs.get("model"), "deepseek-flash")

    def test_system_prompt_keeps_pro_active_bias(self):
        # 口径方向是被反复调过的（放宽→收紧→再放宽），钉住当前值：
        # 2026-09-25 深夜用户拍板「提示词改成活跃即可」——频率管控归管理页
        # 的发言间隔（180 秒），判断口径只管像不像个活人，别再把方向写反
        self.assertIn("拿不准的时候倾向「接」", interject._SYSTEM)
        self.assertNotIn("拿不准就「不接」", interject._SYSTEM)


    def test_judge_is_marked_before_calling(self):
        # 失败也要计入节流，否则调用一直挂会疯狂重试
        with interject._state_lock:
            interject._last_judged.clear()
        p = mock.patch.object(interject.agent_store, "interject_min_gap",
                              return_value=600)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(interject, "call_llm",
                              side_effect=RuntimeError("挂了"))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(interject.log, "exception")
        p.start()
        self.addCleanup(p.stop)
        self.assertIsNone(interject.decide("qq", "1041079621"))
        with interject._state_lock:
            self.assertIn(("qq", "1041079621"), interject._last_judged)


class DecisionLogTest(_StateIsolationMixin, unittest.TestCase):
    """每次接话判断都要在终端打一行结果（带群名）——用户盯适配层窗口用。"""

    def setUp(self):
        super().setUp()
        for name, value in (("QQ_INTERJECT_MODE", "on"),
                            ("QQ_INTERJECT_GROUPS", [])):
            p = mock.patch.object(interject, name, value)
            p.start()
            self.addCleanup(p.stop)
        # 冷却秒数挂在 agent_store（settings 层）；这里设 0 = 不限频，
        # 「接」就直接 pass；test_want_but_cooled_logs_hold 单独覆盖
        p = mock.patch.object(interject.agent_store, "interject_cooldown",
                              return_value=0)
        p.start()
        self.addCleanup(p.stop)
        # 群名走 qq_api 的缓存查询，测试里固定返回
        p = mock.patch.object(interject, "group_display_name",
                              return_value="被子教")
        p.start()
        self.addCleanup(p.stop)
        # 文件落盘挡掉（终端日志才是本组用例的对象）
        p = mock.patch.object(interject, "_log_verdict")
        p.start()
        self.addCleanup(p.stop)

    def _judge(self, answer, last_spoke=None):
        p = mock.patch.object(interject, "call_llm", return_value=answer)
        p.start()
        self.addCleanup(p.stop)
        if last_spoke is not None:
            with interject._state_lock:
                interject._last_spoke[("qq", "1041446471")] = last_spoke
        with self.assertLogs("interject", level="INFO") as cm:
            interject.decide("qq", "1041446471")
        return "\n".join(cm.output)

    def test_pass_logs_open(self):
        out = self._judge("接")
        self.assertIn("接话判断 -> 群聊 [被子教(1041446471)]: 接，开口", out)

    def test_want_but_cooled_logs_hold(self):
        # 正式模式下冷却中的消息不会走到判断（decide 提前返回 None），
        # 这个分支只在影子模式/竞态下出现——直接测格式化函数本身
        with self.assertLogs("interject", level="INFO") as cm:
            interject._log_decision("1041446471", {
                "choice": "接", "want": True, "cooled": False,
                "pass": False, "latency_ms": 1200.0})
        self.assertIn("接话判断 -> 群聊 [被子教(1041446471)]: 接，但冷却中，憋住",
                      "\n".join(cm.output))

    def test_decline_logs_no(self):
        out = self._judge("不接")
        self.assertIn("接话判断 -> 群聊 [被子教(1041446471)]: 不接", out)

    def test_no_name_falls_back_to_id(self):
        # 名单拿不到也得打日志，退回群号
        p = mock.patch.object(interject, "group_display_name", return_value="")
        p.start()
        self.addCleanup(p.stop)
        out = self._judge("接")
        self.assertIn("接话判断 -> 群聊 [1041446471]", out)

class CooldownTest(_StateIsolationMixin, unittest.TestCase):
    """冷却判断走 agent_store.interject_cooldown（settings 层，管理页可调）。"""

    def _cooldown(self, value):
        p = mock.patch.object(interject.agent_store, "interject_cooldown",
                              return_value=value)
        p.start()
        self.addCleanup(p.stop)

    def test_zero_cooldown_always_ok(self):
        self._cooldown(0)
        interject.mark_spoke("qq", "1")
        self.assertTrue(interject._cooldown_ok("qq", "1"))

    def test_cooldown_blocks_then_expires(self):
        self._cooldown(60)
        interject.mark_spoke("qq", "1")
        self.assertFalse(interject._cooldown_ok("qq", "1"))

        with interject._state_lock:
            interject._last_spoke[("qq", "1")] = time.time() - 61
        self.assertTrue(interject._cooldown_ok("qq", "1"))

    def test_cooldown_is_per_group(self):
        self._cooldown(60)
        interject.mark_spoke("qq", "1")
        self.assertTrue(interject._cooldown_ok("qq", "2"))


class ChanceGateTest(_StateIsolationMixin, unittest.TestCase):
    """概率门：没摇中就不许惊动模型。默认 12%（≈1/8），0 / 100 是两端。"""

    def setUp(self):
        super().setUp()
        for name, value in (("QQ_INTERJECT_MODE", "shadow"),
                            ("QQ_INTERJECT_GROUPS", [])):
            p = mock.patch.object(interject, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(interject.recent, "format_recent",
                              return_value="张三：在吗")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(interject, "_log_verdict")
        p.start()
        self.addCleanup(p.stop)

    def _chance(self, value):
        p = mock.patch.object(interject.agent_store, "interject_chance",
                              return_value=value)
        p.start()
        self.addCleanup(p.stop)

    def _rand(self, value):
        """钉住骰子：只换 interject 眼里那个 random，不动全局。"""
        p = mock.patch.object(interject, "random",
                              mock.Mock(random=lambda: value))
        p.start()
        self.addCleanup(p.stop)

    def _decide(self):
        with mock.patch.object(interject, "call_llm",
                               return_value="接") as llm:
            return interject.decide("qq", "1"), llm

    def test_zero_never_judges(self):
        self._chance(0)
        self._rand(0.0)
        v, llm = self._decide()
        self.assertIsNone(v)
        llm.assert_not_called()

    def test_hundred_always_judges(self):
        self._chance(100)
        self._rand(0.99)
        v, llm = self._decide()
        self.assertIsNotNone(v)
        llm.assert_called_once()

    def test_roll_above_chance_is_skipped(self):
        self._chance(12)          # 摇出 50%，比 12% 大 → 不判
        self._rand(0.5)
        v, llm = self._decide()
        self.assertIsNone(v)
        llm.assert_not_called()

    def test_roll_within_chance_judges(self):
        self._chance(50)          # 摇出 20%，比 50% 小 → 判
        self._rand(0.2)
        v, llm = self._decide()
        self.assertIsNotNone(v)
        llm.assert_called_once()

    def test_gate_leaves_no_stamp_when_skipped(self):
        # 摇不中连时间戳都不能记：否则「刚判过」会把它后面真正该判的也挡掉
        self._chance(0)
        self._rand(0.0)
        self._decide()
        with interject._state_lock:
            self.assertNotIn(("qq", "1"), interject._last_judged)

    def test_default_is_about_one_in_eight(self):
        # 默认 12%：主动开口是点缀，绝大多数消息不该付这次调用的钱
        self.assertEqual(agent_store.DEFAULT_INTERJECT_CHANCE, 12)


class GapFromSettingsTest(_StateIsolationMixin, unittest.TestCase):
    """判断间隔改从 settings 层读（管理页可调），不再吃 .env 的常量。"""

    def _gap(self, value):
        p = mock.patch.object(interject.agent_store, "interject_min_gap",
                              return_value=value)
        p.start()
        self.addCleanup(p.stop)

    def test_zero_gap_always_ok(self):
        self._gap(0)
        interject._mark_judged("qq", "1")
        self.assertTrue(interject._gap_ok("qq", "1"))

    def test_gap_blocks_then_expires(self):
        self._gap(60)
        interject._mark_judged("qq", "1")
        self.assertFalse(interject._gap_ok("qq", "1"))
        with interject._state_lock:
            interject._last_judged[("qq", "1")] = time.time() - 61
        self.assertTrue(interject._gap_ok("qq", "1"))

    def test_gap_is_per_group(self):
        self._gap(60)
        interject._mark_judged("qq", "1")
        self.assertTrue(interject._gap_ok("qq", "2"))


class ShadowLogTest(_StateIsolationMixin, unittest.TestCase):
    """影子模式全记（要看它「不接」判得对不对）；正式模式只记被冷却挡掉的。"""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(interject, "QQ_INTERJECT_GROUPS", [])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(interject.recent, "format_recent",
                              return_value="张三：在吗")
        p.start()
        self.addCleanup(p.stop)

    def _log(self):
        p = mock.patch.object(interject, "_log_verdict")
        return p.start(), p

    def test_shadow_logs_declines_too(self):
        with mock.patch.object(interject, "QQ_INTERJECT_MODE", "shadow"):
            log_verdict, p = self._log()
            self.addCleanup(p.stop)
            with mock.patch.object(interject, "call_llm", return_value="不接"):
                interject.decide("qq", "1")
            self.assertEqual(log_verdict.call_count, 1)

    def test_on_does_not_log_plain_declines(self):
        with mock.patch.object(interject, "QQ_INTERJECT_MODE", "on"):
            log_verdict, p = self._log()
            self.addCleanup(p.stop)
            with mock.patch.object(interject, "call_llm", return_value="不接"):
                interject.decide("qq", "1")
            log_verdict.assert_not_called()

    def test_shadow_still_judges_and_logs_during_cooldown(self):
        # on 模式冷却中连判断都不做（见 DecideGuardTest），影子模式不跳：
        # 观察期就是要把每条消息的判断都记下来，冷却照走、日志照记
        with mock.patch.object(interject, "QQ_INTERJECT_MODE", "shadow"):
            log_verdict, p = self._log()
            self.addCleanup(p.stop)
            interject.mark_spoke("qq", "1")
            with mock.patch.object(interject, "call_llm", return_value="接"):
                v = interject.decide("qq", "1")
            self.assertEqual(log_verdict.call_count, 1)
            self.assertTrue(v["want"])
            self.assertFalse(v["cooled"])

    def test_shadow_does_not_burn_cooldown_when_it_cannot_speak(self):
        # 影子模式不发言，但它照样走冷却——这样日志里「会开口」的次数
        # 就等于放开后真实的发言次数，可用来估会不会刷屏
        with mock.patch.object(interject, "QQ_INTERJECT_MODE", "shadow"):
            _, p = self._log()
            self.addCleanup(p.stop)
            with mock.patch.object(interject, "call_llm", return_value="接"):
                interject.decide("qq", "1")
            self.assertFalse(interject._cooldown_ok("qq", "1"))


class RunTurnVoluntaryTest(_StateIsolationMixin, unittest.TestCase):
    """worker 线程里：这批消息没点名机器人时，先问判断模型再决定跑不跑。"""

    def setUp(self):
        super().setUp()
        # 默认「群里没有最近的图」，免得用例读到真实缓存里的图地址
        p = mock.patch.object(qq_bot.recent, "latest_image", return_value="")
        p.start()
        self.addCleanup(p.stop)

    def _runner(self):
        return qq_bot.SessionRunner(None, "group_1041079621", "group",
                                    "1041079621")

    def _capture_merge(self):
        seen = {}

        def fake_merge(batch, **kw):
            seen["batch"] = list(batch)
            return ""        # 空正文 → 后续提前返回，不进 LLM
        p = mock.patch.object(qq_bot, "_merge_batch", side_effect=fake_merge)
        p.start()
        self.addCleanup(p.stop)
        return seen

    def test_declined_batch_does_not_run(self):
        runner = self._runner()
        seen = self._capture_merge()
        with mock.patch.object(interject, "decide", return_value=None):
            runner._run_turn([{"text": "在吗", "sender": "张三",
                               "images": [], "quotes": [], "tentative": True}])
        self.assertNotIn("batch", seen)          # 压根没走到拼正文

    def test_accepted_batch_is_replaced_by_prompt(self):
        runner = self._runner()
        seen = self._capture_merge()
        verdict = {"choice": "接", "want": True, "cooled": True, "pass": True,
                   "latency_ms": 10.0, "context_chars": 10, "raw": "接"}
        with mock.patch.object(interject, "decide", return_value=verdict), \
                mock.patch.object(interject, "speaking", return_value=True), \
                mock.patch.object(qq_bot.recent, "recent_image_records",
                                  return_value=[]):
            runner._run_turn([{"text": "在吗", "sender": "张三",
                               "images": [], "quotes": [], "tentative": True}])
        # 关键：群消息不能被当成「有人在问它」，正文换成了那句说明
        batch = seen["batch"]
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["text"], interject.INTERJECT_PROMPT)
        self.assertEqual(batch[0]["images"], [])
        self.assertEqual(batch[0]["quotes"], [])

    def test_shadow_mode_does_not_run_even_when_accepted(self):
        runner = self._runner()
        seen = self._capture_merge()
        verdict = {"choice": "接", "want": True, "cooled": True, "pass": True,
                   "latency_ms": 10.0, "context_chars": 10, "raw": "接"}
        with mock.patch.object(interject, "decide", return_value=verdict), \
                mock.patch.object(interject, "speaking", return_value=False):
            runner._run_turn([{"text": "在吗", "sender": "张三",
                               "images": [], "quotes": [], "tentative": True}])
        self.assertNotIn("batch", seen)

    def test_voluntary_reply_sees_latest_images(self):
        # 判断模型只看得到 "[图片]" 占位符；判「接」之后要把最近两张真正的图
        # 带给主模型（群里经常连着甩表情，一张常常不够），而且得带署名——
        # 不告诉模型图是谁发的，它会把图安到最近在发言的那个人头上
        runner = self._runner()
        seen = self._capture_merge()
        verdict = {"choice": "接", "want": True, "cooled": True, "pass": True,
                   "latency_ms": 10.0, "context_chars": 10, "raw": "接"}
        with mock.patch.object(interject, "decide", return_value=verdict), \
                mock.patch.object(interject, "speaking", return_value=True), \
                mock.patch.object(qq_bot.recent, "recent_image_records",
                                  return_value=[
                                      {"m": "http://x/pic2.jpg",
                                       "n": "被子教"},
                                      {"m": "http://x/pic1.jpg",
                                       "n": "猫大侠"}]) as ri:
            runner._run_turn([{"text": "在吗", "sender": "张三",
                               "images": [], "quotes": [], "tentative": True}])
        ri.assert_called_once_with("qq", "1041079621",
                                   qq_bot._INTERJECT_IMAGE_LOOKBACK, 2)
        self.assertEqual(seen["batch"][0]["images"],
                         ["http://x/pic2.jpg", "http://x/pic1.jpg"])
        self.assertIn("被子教", seen["batch"][0]["text"])
        self.assertIn("猫大侠", seen["batch"][0]["text"])

    def test_mixed_batch_goes_the_normal_way(self):
        # 只要混进一条被 @ 的，就照常回，不必问判断模型
        runner = self._runner()
        seen = self._capture_merge()
        with mock.patch.object(interject, "decide") as decide:
            runner._run_turn([
                {"text": "在吗", "sender": "张三", "images": [], "quotes": [],
                 "tentative": True},
                {"text": "张三：@机器人 你好", "sender": "张三", "images": [],
                 "quotes": [], "tentative": False},
            ])
        decide.assert_not_called()
        self.assertIn("batch", seen)
        self.assertEqual(len(seen["batch"]), 2)


class DispatchTentativeTest(_StateIsolationMixin, unittest.TestCase):
    """_dispatch 侧：不 @ 也没命中触发词时，把它交给判断链路而不是丢掉。"""

    def _bot(self):
        bot = qq_bot.QQBot()
        submitted = []

        class _Runner:
            def submit(self, text, sender="", images=None, quotes=None,
                       tentative=False, user_id=""):
                submitted.append({"text": text, "sender": sender,
                                  "tentative": tentative,
                                  "user_id": user_id})

        p = mock.patch.object(bot, "_runner_for",
                              return_value=_Runner())
        p.start()
        self.addCleanup(p.stop)
        return bot, submitted

    def setUp(self):
        super().setUp()
        p = mock.patch.object(qq_bot.recent, "remember")
        p.start()
        self.addCleanup(p.stop)

    def test_disabled_feature_still_drops(self):
        bot, submitted = self._bot()
        with mock.patch.object(qq_bot.interject, "enabled",
                               return_value=False):
            bot._dispatch(_group_event("大家好啊"))
        self.assertEqual(submitted, [])

    def test_enabled_feature_hands_it_over(self):
        bot, submitted = self._bot()
        with mock.patch.object(qq_bot.interject, "enabled", return_value=True):
            bot._dispatch(_group_event("大家好啊"))
        self.assertEqual(len(submitted), 1)
        self.assertTrue(submitted[0]["tentative"])

    def test_at_without_content_is_dropped_not_tentative(self):
        # @ 了机器人却什么都没发（按错了、或话还没说完）。让机器人凭空开口很
        # 奇怪，这种情况该照旧丢掉——但它很容易被误判成「没人叫它」：_should_reply
        # 对「@ 了没内容」和「没 @」都返回 False，只看 bool 的话两条路会合流。
        bot, submitted = self._bot()
        raw = json.dumps({
            "post_type": "message", "message_type": "group",
            "group_id": 1041079621, "user_id": 111, "self_id": 999,
            "sender": {"nickname": "张三"},
            "message": [{"type": "at", "data": {"qq": "999"}}],
        })
        with mock.patch.object(qq_bot.interject, "enabled", return_value=True):
            bot._dispatch(raw)
        self.assertEqual(submitted, [])

    def test_blacklisted_user_is_dropped(self):
        # 用户明确划的界优先于「要不要主动接话」
        bot, submitted = self._bot()
        p = mock.patch.object(qq_bot, "QQ_BLACKLIST_USERS", ["111"])
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(qq_bot.interject, "enabled", return_value=True):
            bot._dispatch(_group_event("大家好啊"))
        self.assertEqual(submitted, [])

    def test_group_not_whitelisted_is_dropped(self):
        bot, submitted = self._bot()
        p = mock.patch.object(qq_bot, "QQ_WHITELIST_GROUPS", ["999"])
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(qq_bot.interject, "enabled", return_value=True):
            bot._dispatch(_group_event("大家好啊"))
        self.assertEqual(submitted, [])

    def test_at_me_is_not_tentative(self):
        bot, submitted = self._bot()
        raw = json.dumps({
            "post_type": "message", "message_type": "group",
            "group_id": 1041079621, "user_id": 111, "self_id": 999,
            "sender": {"nickname": "张三"},
            "message": [
                {"type": "at", "data": {"qq": "999"}},
                {"type": "text", "data": {"text": "你好"}},
            ],
        })
        with mock.patch.object(qq_bot.interject, "enabled", return_value=True):
            bot._dispatch(raw)
        self.assertEqual(len(submitted), 1)
        self.assertFalse(submitted[0]["tentative"])

    def test_own_message_is_skipped_entirely(self):
        bot, submitted = self._bot()
        with mock.patch.object(qq_bot.interject, "enabled", return_value=True):
            bot._dispatch(_group_event("我自己说的", user_id=999, self_id=999))
        self.assertEqual(submitted, [])

    def test_private_is_untouched(self):
        # 主动接话只做群聊：私聊里没人点名也会回，等于骚扰。私聊照旧走原来的
        # 路（正常回复），不能被打上 tentative 标记。
        bot, submitted = self._bot()
        raw = json.dumps({
            "post_type": "message", "message_type": "private",
            "user_id": 111, "self_id": 999,
            "sender": {"nickname": "张三"},
            "message": [{"type": "text", "data": {"text": "你好"}}],
        })
        with mock.patch.object(qq_bot.interject, "enabled", return_value=True):
            bot._dispatch(raw)
        self.assertEqual(len(submitted), 1)
        self.assertFalse(submitted[0]["tentative"])


if __name__ == "__main__":
    unittest.main()
