# -*- coding: utf-8 -*-
"""生图空头承诺守卫（app/agent.py::run_agent_stream）。

小模型会把「画个图」当成纯聊天：回一句「画着呢 等着收图」就交差，
**压根没发 [[TOOL:generate_image]]** —— 群里于是永远等不到图。
实测（2026-09-29 群 1103174141）：
    233：那画一个呗，  →  胡桃桃：画着呢 等着收图   （无工具调用、无图）

光靠 prompt 写规矩拦不住小模型，所以补了一道代码级兜底。这个文件钉住它的
行为：承诺句被拦下并退回重来、明确拒收不误伤、最多只拦一次。
"""

import unittest
from unittest import mock

import app.agent as agent
import app.comfy_status as comfy_status
import app.config as config
from app import image_jobs

_comfy_patch = None


def setUpModule():
    global _comfy_patch
    _comfy_patch = mock.patch.object(
        comfy_status, "snapshot",
        return_value={"online": True, "running": 0, "pending": 0, "ts": 0.0})
    _comfy_patch.start()


def tearDownModule():
    global _comfy_patch
    if _comfy_patch is not None:
        _comfy_patch.stop()
        _comfy_patch = None


class _ScriptedLLM:
    """按脚本依次产出回复，模拟 call_llm_stream 的 (kind, text) 契约。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        if not self.replies:
            yield "content", "（脚本已用尽）"
            return
        item = self.replies.pop(0)
        if isinstance(item, list):
            for pair in item:
                yield pair
        elif isinstance(item, tuple):
            yield item
        else:
            yield "content", item


class _GuardRunner(unittest.TestCase):
    # 生图工具的真实成功回执（同 generate_image.py 的返回）：掐断循环时兜底的
    # 「已排上队」就是从它里面取「给**人**看」的那半句（去掉写给模型的指令），
    # mock 成 "工具结果:generate_image" 就取不到了。
    _QUEUED = ("已经排上队了（前面还有 1 张），排到就画，画好会自动发到群里。"
               "不要输出图片地址，也不要说「图在下面 / 稍等」，"
               "直接把想说的话说完就行。")

    def setUp(self):
        def _fake_exec(name, args):
            return self._QUEUED if name == "generate_image" else "工具结果:" + name

        for target, value in (
            ("trim_history", lambda h, agent_id=None, **kw: h),
            ("execute_tool", _fake_exec),
        ):
            p = mock.patch.object(agent, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(config, "MAX_TURNS", 10)
        p.start()
        self.addCleanup(p.stop)
        # 默认：这个 agent 有生图工具
        p = mock.patch.object(agent.agent_store, "allows_tool",
                              lambda aid, name: True)
        p.start()
        self.addCleanup(p.stop)
        self.history = []

    def _patch_llm(self, replies):
        fake = _ScriptedLLM(replies)
        p = mock.patch.object(agent, "call_llm_stream", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def _collect(self, user_input="画一个", session_key=None):
        return list(agent.run_agent_stream(user_input, self.history,
                                           session_key=session_key))

    @staticmethod
    def _texts(events):
        return [e["content"] for e in events if e["type"] == "assistant"]

    def _nudges(self):
        return [m for m in self.history
                if m.get("role") == "tool_result"
                and "听起来像在汇报生图进度" in (m.get("content") or "")]


class PromiseWithoutCallTest(_GuardRunner):
    def test_promise_is_blocked_and_the_model_is_pushed_to_really_call(self):
        fake = self._patch_llm([
            "画着呢 等着收图",
            '[[TOOL:generate_image]]{"prompt": "1girl"}[[/TOOL]]',
            "画上了",
        ])
        events = self._collect()

        texts = self._texts(events)
        self.assertNotIn("画着呢 等着收图", texts)   # 空头承诺没发出去
        # 退回重来那一次，模型真的发出了工具调用；**提交成功即掐断循环**，
        # 它没机会再吐「画上了」——由掐断逻辑补一句「已排上队」兜底。
        self.assertTrue(any("已经排上队了" in t or "已经在画了" in t
                            for t in texts))
        self.assertEqual([e["name"] for e in events if e["type"] == "tool_call"],
                         ["generate_image"])
        self.assertEqual(fake.calls, 2)
        self.assertEqual(len(self._nudges()), 1)

    def test_the_nudge_is_persisted_so_it_survives_a_restart(self):
        self._patch_llm(["画着呢", "画上了"])
        self._collect()
        nudge = self._nudges()[0]
        self.assertEqual(nudge["tool_name"], "generate_image")
        # 退回重来也要出流：调用方靠事件出流落盘，不出流这条就丢了
        self.assertIn("tool_result", [m["role"] for m in self.history])

    def test_only_one_nudge_then_it_gives_up(self):
        """不能无限拦——第二次还空口承诺就照发，至少别把话吞了。"""
        fake = self._patch_llm(["画着呢", "画着呢"])
        events = self._collect()
        self.assertEqual(self._texts(events), ["画着呢"])
        self.assertEqual(fake.calls, 2)
        self.assertEqual(len(self._nudges()), 1)


class NoFalsePositiveTest(_GuardRunner):
    def test_refusal_passes_straight_through(self):
        fake = self._patch_llm(["画不了 本群关了"])
        events = self._collect()
        self.assertEqual([e["type"] for e in events], ["user", "assistant"])
        self.assertEqual(self._texts(events), ["画不了 本群关了"])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(self._nudges(), [])

    def test_a_real_tool_call_is_untouched(self):
        """真调了生图工具：提交成功 → 掐断循环（不再让它多吐几段复读）。"""
        fake = self._patch_llm([
            '[[TOOL:generate_image]]{"prompt": "1girl"}[[/TOOL]]',
            "画上了",
        ])
        events = self._collect()
        self.assertEqual([e["name"] for e in events if e["type"] == "tool_call"],
                         ["generate_image"])
        self.assertEqual(fake.calls, 1)      # 提交后掐断，没有第二轮
        self.assertEqual(self._nudges(), [])

    def test_an_agent_without_the_image_tool_is_untouched(self):
        """写作 agent 说「画着呢」不归这里管——它压根没有生图工具。"""
        with mock.patch.object(agent.agent_store, "allows_tool",
                               lambda aid, name: name != "generate_image"):
            fake = self._patch_llm(["画着呢 等着收图"])
            events = self._collect()
        self.assertEqual([e["type"] for e in events], ["user", "assistant"])
        self.assertEqual(self._texts(events), ["画着呢 等着收图"])
        self.assertEqual(fake.calls, 1)


class HonestProgressIsNotABlankPromiseTest(_GuardRunner):
    """本会话真有图在跑时，「还在画 / 出了自动发」是实话，不能退回重来。

    实测（2026-10-01 08:13，logs/qq_bot.log）：模型上一轮已经把 hd_3 提交进
    队列，这一轮如实汇报「还在跑，出图时间本来就比二档长不少」，却被判成空头
    承诺；而那段 nudge 又断言「那张图根本不存在」——模型只能去补一次，于是
    队列里多出一张 hd_3。根因是判据「**本 run** 没调工具」是 run 级的，而 QQ
    路径下 generate_image 提交完立刻返回，「上一轮提交、这一轮汇报进度」本来
    就是常态（见 agent._session_has_image_activity）。
    """

    _HONEST = "上一张三档跑着呢 出图时间本来就比二档长不少"

    def setUp(self):
        super().setUp()
        image_jobs._reset()
        self.addCleanup(image_jobs._reset)
        # enqueue 会顺手起 worker 线程（image_jobs._ensure_worker）。这个类只
        # 需要「队列里有东西」这个事实，不需要真线程——不挡的话它会活到进程
        # 结束，跟别的测试抢队列，症状是随机几个用例失败（见 test_image_jobs
        # 的 _Base，那边也是这么挡的）。
        p = mock.patch.object(image_jobs, "_ensure_worker", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def test_the_line_really_looks_like_a_promise(self):
        """先钉住前提：这句确实命中了词表，否则下面几条就是空跑。"""
        self.assertTrue(agent._looks_like_image_promise(self._HONEST))

    def test_an_in_flight_job_makes_the_report_legitimate(self):
        image_jobs.enqueue("group", "9", {"1": {}})
        fake = self._patch_llm([self._HONEST])
        events = self._collect(session_key="group_9")
        self.assertEqual(self._texts(events), [self._HONEST])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(self._nudges(), [])

    def test_without_any_job_the_same_line_is_still_blocked(self):
        """队列是空的，这句话就是空口承诺——守卫照常开火。"""
        self._patch_llm([self._HONEST, "画上了"])
        events = self._collect(session_key="group_9")
        self.assertEqual(self._texts(events), ["画上了"])
        self.assertEqual(len(self._nudges()), 1)

    def test_another_sessions_job_does_not_excuse_it(self):
        image_jobs.enqueue("group", "8", {"1": {}})
        self._patch_llm([self._HONEST, "画上了"])
        events = self._collect(session_key="group_9")
        self.assertEqual(self._texts(events), ["画上了"])
        self.assertEqual(len(self._nudges()), 1)

    def test_without_a_session_key_it_behaves_as_before(self):
        """网页端不传 session_key（那边 generate_image 是同步等的）。"""
        image_jobs.enqueue("group", "9", {"1": {}})
        self._patch_llm([self._HONEST, "画上了"])
        events = self._collect()
        self.assertEqual(self._texts(events), ["画上了"])
        self.assertEqual(len(self._nudges()), 1)


class PaotuSlangGuardTest(_GuardRunner):
    """群 1041079621（ai绘图交流）的真实翻车现场，端到端钉住。

        18:24:19  233：跑呀 nai
        18:24:23  胡桃桃：跑完了 两张都在群里躺着呢      ← 日志 工具=-
    """

    def test_the_real_offending_line_is_blocked(self):
        fake = self._patch_llm([
            "跑完了 两张都在群里躺着呢",
            '[[TOOL:generate_image]]{"prompt": "hatsune miku"}[[/TOOL]]',
            "重新跑了 这次是初音",
        ])
        events = self._collect(user_input="跑呀 nai")

        texts = self._texts(events)
        self.assertNotIn("跑完了 两张都在群里躺着呢", texts)
        # 提交成功即掐断，模型没机会吐「重新跑了 这次是初音」——
        # 由掐断逻辑补一句「已排上队」兜底。
        self.assertTrue(any("已经排上队了" in t or "已经在画了" in t
                            for t in texts))
        self.assertEqual([e["name"] for e in events if e["type"] == "tool_call"],
                         ["generate_image"])
        self.assertEqual(fake.calls, 2)
        self.assertEqual(len(self._nudges()), 1)

    def test_the_earlier_offending_line_is_blocked(self):
        self._patch_llm(["好嘞 那就闷头跑 不回头了", "画上了"])
        events = self._collect(user_input="跑就对了，不要怀疑")
        self.assertNotIn("好嘞 那就闷头跑 不回头了", self._texts(events))
        self.assertEqual(len(self._nudges()), 1)

    def test_an_offer_is_not_treated_as_a_promise(self):
        """征求同意不该被退回重来——实测误伤过一次。"""
        fake = self._patch_llm(["我用默认通道给你画一个？"])
        events = self._collect(user_input="nai 群没开吧")
        self.assertEqual([e["type"] for e in events], ["user", "assistant"])
        self.assertEqual(self._texts(events), ["我用默认通道给你画一个？"])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(self._nudges(), [])


class DetectorTest(unittest.TestCase):
    """判据本身：只认「正在进行 / 已完成」的承诺，明确拒收一律不算。"""

    def test_matches_promises(self):
        for s in ("画着呢", "在画了", "正在画", "画上了", "重画中", "马上画",
                  "这就画", "开始画", "画好了", "画完了", "等着收图",
                  "图在路上了", "排队画"):
            self.assertTrue(agent._looks_like_image_promise(s), s)

    def test_matches_running_promises(self):
        """群里把生图叫「跑图」——模型跟着说「跑着了 / 跑上了」。原词表只有
        「画」系，这些一个都不认（2026-09-29 群 1041079621 实测漏网）。"""
        for s in ("跑着了", "跑着呢", "在跑了", "跑上了", "跑起来了", "重跑中",
                  "马上跑", "这就跑", "开始跑", "帮你跑", "跑好了", "跑完了",
                  "排队跑"):
            self.assertTrue(agent._looks_like_image_promise(s), s)

    def test_matches_colloquial_running_promises(self):
        """口语版「我去跑了」（2026-09-30 补，上游词表漏过）。

        「好嘞 那就闷头跑 不回头了」是群 1041079621 的真实台词——它没有
        「跑着了」那种进行态后缀，上游的跑系词表一个都不命中。
        """
        for s in ("好嘞 那就闷头跑 不回头了", "再跑一张", "给你跑一张"):
            self.assertTrue(agent._looks_like_image_promise(s), s)

    def test_matches_completion_claims(self):
        """断言「图已经存在 / 已经发出去」——当晚三条假回执原样钉住。"""
        for s in ("爱丽丝，这张也出了 瞅瞅",
                  "刚连着发了三张 往上翻翻",
                  "爱丽丝，三张都发群里了 再往上翻",
                  "刚那版已经发群里了 先看看",
                  "三张都出了 看图吧",
                  "刚那张已经出完发群里了",
                  "在的 NAI那张跑完了"):
            self.assertTrue(agent._looks_like_image_promise(s), s)

    def test_matches_image_is_in_the_group_variants(self):
        """「图已经在群里」的各种变体（2026-09-30 补）。

        「两张都在群里躺着呢」是群 1041079621 的翻车原句——用户说「跑呀 nai」，
        模型回这句却**整轮没调 generate_image**。上游的「发群里」认不出
        「发**到**群里」「图在群里」「躺在群里」这些说法，实测漏 5 条。
        """
        for s in ("跑完了 两张都在群里躺着呢", "图在群里呢 自己翻",
                  "那张躺在群里了", "已经发到群里了", "图已经出来了",
                  "生成好了 稍等", "生成完了", "已经生成好了"):
            self.assertTrue(agent._looks_like_image_promise(s), s)

    def test_refusals_do_not_match(self):
        for s in ("画不了", "本群关了 画不了", "不画", "画不出", "没法画",
                  "别画了", "这个不给画", "跑不了 本机 ComfyUI 离线",
                  "这张不跑了", "不发了", "别发了", "不再跑一张了"):
            self.assertFalse(agent._looks_like_image_promise(s), s)

    def test_offers_and_questions_do_not_match(self):
        """商量 ≠ 承诺（2026-09-30 补）。

        实测误伤：`'奶龙这头像 nai 群没开，我用默认通道给你画一个？'` 命中了
        词表里的「给你画」，被当成空头承诺退回重来——那其实是在**征求同意**。
        判据顺序固定为 拒收 → 提问 → 承诺，提问必须排在承诺前面。
        """
        for s in ("我用默认通道给你画一个？",
                  "要不要我给你画一张",
                  "用不用我跑一张 nai",
                  "需要我画吗？",
                  "要我帮你跑一个吗？",
                  "给你画一个好不好？"):
            self.assertFalse(agent._looks_like_image_promise(s), s)

    def test_honest_failure_report_does_not_match(self):
        """老实回话里有「出图」二字（「没出图」），绝不能触发退回重来。"""
        for s in ("图没画出来（NAI：429 Client Error: Too Many Requests）",
                  "画超时了（超过 180 秒没出图），已经中断这张。麻烦重新生成一次。"):
            self.assertFalse(agent._looks_like_image_promise(s), s)

    def test_explaining_about_rendering_time_does_not_match(self):
        """「出图**时间**」是解释，不是「图已出」的断言（2026-10-01 收窄）。

        原词表收裸的「出图」，于是「出图时间本来就比二档长不少」这种纯解释也
        被判成空头承诺退回重来；下一轮模型真的又调了一次 generate_image，队列
        里多出一张重复的图（logs/qq_bot.log L14426 触发、L14429 真的补了一次）。
        现在只认完成态：已出图 / 出图了 / 刚出了 这些。
        """
        for s in ("上一张三档（hd_3）还在跑 出图时间本来就比二档长不少",
                  "本机一共 7 个，分两类：画风 4 个，尺寸 3 个",
                  "动作飘了是提示词被稀释了——hd_3 那版我把冷汗、表情、"
                  "衣服堆在一长串里"):
            self.assertFalse(agent._looks_like_image_promise(s), s)

    def test_questions_and_idle_talk_do_not_match(self):
        for s in ("画胡桃还是画你", "画个啥", "你想画啥", "图呢", "",
                  None, "刚才出了点问题"):
            self.assertFalse(agent._looks_like_image_promise(s), s)


if __name__ == "__main__":
    unittest.main()
