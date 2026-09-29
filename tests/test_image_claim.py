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
    def setUp(self):
        for target, value in (
            ("trim_history", lambda h, agent_id=None, **kw: h),
            ("execute_tool", lambda name, args: "工具结果:" + name),
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

    def _collect(self, user_input="画一个"):
        return list(agent.run_agent_stream(user_input, self.history))

    @staticmethod
    def _texts(events):
        return [e["content"] for e in events if e["type"] == "assistant"]

    def _nudges(self):
        return [m for m in self.history
                if m.get("role") == "tool_result"
                and "没有真正调用" in (m.get("content") or "")]


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
        self.assertIn("画上了", texts)               # 重来后正常收尾
        # 退回重来那一次，模型真的发出了工具调用
        self.assertEqual([e["name"] for e in events if e["type"] == "tool_call"],
                         ["generate_image"])
        self.assertEqual(fake.calls, 3)
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
        fake = self._patch_llm([
            '[[TOOL:generate_image]]{"prompt": "1girl"}[[/TOOL]]',
            "画上了",
        ])
        events = self._collect()
        self.assertEqual([e["name"] for e in events if e["type"] == "tool_call"],
                         ["generate_image"])
        self.assertEqual(fake.calls, 2)
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


class DetectorTest(unittest.TestCase):
    """判据本身：只认「正在进行 / 已完成」的承诺，明确拒收一律不算。"""

    def test_matches_promises(self):
        for s in ("画着呢", "在画了", "正在画", "画上了", "重画中", "马上画",
                  "这就画", "开始画", "画好了", "画完了", "等着收图",
                  "图在路上了", "排队画"):
            self.assertTrue(agent._looks_like_image_promise(s), s)

    def test_refusals_do_not_match(self):
        for s in ("画不了", "本群关了 画不了", "不画", "画不出", "没法画",
                  "别画了", "这个不给画"):
            self.assertFalse(agent._looks_like_image_promise(s), s)

    def test_questions_and_idle_talk_do_not_match(self):
        for s in ("画胡桃还是画你", "画个啥", "你想画啥", "图呢", "", None):
            self.assertFalse(agent._looks_like_image_promise(s), s)


if __name__ == "__main__":
    unittest.main()
