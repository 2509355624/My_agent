# -*- coding: utf-8 -*-
"""Agent 主循环测试（app/agent.py::run_agent_stream）。

用"脚本化的假 LLM"替换真实网络调用，验证事件流顺序、工具执行、
pre_tool_results 注入、异常兜底与最大轮次保护。这些是前端渲染和
用户体验直接依赖的行为，回归代价最高。
"""

import unittest
from unittest import mock

import app.agent as agent
import app.config as config


class _ScriptedLLM:
    """按脚本依次返回回复；脚本元素是 Exception 时抛出。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.seen_histories = []

    def __call__(self, messages, **kwargs):
        self.calls += 1
        self.seen_histories.append(messages)
        if not self.replies:
            return "（脚本已用尽）"
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        # 不做上下文压缩、不做真实工具调用、不做真实网络请求
        for target, value in (
            ("trim_history", lambda h: h),
            ("execute_tool", lambda name, args: "工具结果:" + name),
        ):
            p = mock.patch.object(agent, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(config, "MAX_TURNS", 10)
        p.start()
        self.addCleanup(p.stop)
        self.history = []

    def _patch_llm(self, replies):
        fake = _ScriptedLLM(replies)
        p = mock.patch.object(agent, "call_llm", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def _collect(self, user_input, **kwargs):
        return list(agent.run_agent_stream(user_input, self.history, **kwargs))

    def test_plain_answer_event_sequence(self):
        self._patch_llm(["你好，我是助理。"])
        events = self._collect("你好")
        self.assertEqual([e["type"] for e in events], ["user", "assistant"])
        self.assertEqual(events[0]["content"], "你好")
        self.assertEqual(events[1]["content"], "你好，我是助理。")
        self.assertEqual(self.history[0], {"role": "user", "content": "你好"})
        self.assertEqual(self.history[-1]["role"], "assistant")

    def test_single_tool_then_answer(self):
        self._patch_llm(['[[TOOL:get_time]][[/TOOL]]', '现在是 12:00。'])
        events = self._collect("几点了")
        self.assertEqual([e["type"] for e in events],
                         ["user", "tool_call", "tool_result", "assistant"])
        self.assertEqual(events[1]["name"], "get_time")
        self.assertEqual(events[2]["result"], "工具结果:get_time")
        self.assertEqual(events[3]["content"], "现在是 12:00。")
        # 工具结果必须落进历史，下一轮 LLM 才看得到
        self.assertIn("tool_result", [m["role"] for m in self.history])

    def test_tool_call_reply_emits_no_assistant_event(self):
        # 纯工具调用的那一轮，正文被剥空 → 不应产生 assistant 事件
        self._patch_llm(['[[TOOL:get_time]][[/TOOL]]', '好了'])
        events = self._collect("几点")
        self.assertEqual([e["type"] for e in events].count("assistant"), 1)

    def test_multiple_tools_in_one_reply(self):
        self._patch_llm([
            '[[TOOL:get_time]][[/TOOL]]\n[[TOOL:list_skills]][[/TOOL]]',
            '完成',
        ])
        events = self._collect("都查一下")
        self.assertEqual([e["type"] for e in events],
                         ["user", "tool_call", "tool_result",
                          "tool_call", "tool_result", "assistant"])
        self.assertEqual(events[1]["name"], "get_time")
        self.assertEqual(events[3]["name"], "list_skills")

    def test_pre_tool_results_injected_before_loop(self):
        self._patch_llm(["已知答案"])
        pre = [{"name": "get_time", "result": "2026-01-01 00:00:00"}]
        events = self._collect("现在几点", pre_tool_results=pre)
        self.assertEqual([e["type"] for e in events],
                         ["user", "tool_result", "assistant"])
        self.assertEqual(events[1]["name"], "get_time")
        self.assertEqual(events[1]["result"], "2026-01-01 00:00:00")
        # 注入的工具结果要排在 LLM 循环之前（history[1]）
        self.assertEqual(self.history[1]["role"], "tool_result")
        self.assertEqual(self.history[1]["tool_name"], "get_time")

    def test_status_bar_appended_at_tail_not_front(self):
        fake = self._patch_llm(["ok"])
        self.history.append({"role": "system", "content": "stable prompt"})
        self._collect("你好")
        sent = fake.seen_histories[0]
        self.assertEqual(sent[0]["role"], "system")
        # 动态状态栏必须追加在末尾；放在头部会毒化 prefix cache
        self.assertIn("<status_bar>", sent[-1]["content"])
        self.assertNotIn("<status_bar>", sent[0]["content"])

    def test_llm_exception_is_reported_not_raised(self):
        self._patch_llm([RuntimeError("boom")])
        events = self._collect("你好")
        self.assertEqual(events[-1]["type"], "assistant")
        self.assertIn("执行出错", events[-1]["content"])
        self.assertIn("boom", events[-1]["content"])

    def test_max_turns_guard(self):
        p = mock.patch.object(config, "MAX_TURNS", 1)
        p.start()
        self.addCleanup(p.stop)
        self._patch_llm(['[[TOOL:get_time]][[/TOOL]]'])  # 每轮都要调工具
        events = self._collect("一直查")
        self.assertIn("最大轮次", events[-1]["content"])


if __name__ == "__main__":
    unittest.main()
