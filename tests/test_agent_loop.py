# -*- coding: utf-8 -*-
"""Agent 主循环测试（app/agent.py::run_agent_stream）。

用"脚本化的假 LLM"替换真实网络调用，验证事件流顺序、工具执行、
pre_tool_results 注入、思考内容（reasoning）只出不进、异常兜底与
最大轮次保护。这些是前端渲染和用户体验直接依赖的行为，回归代价最高。
"""

import unittest
from unittest import mock

import app.agent as agent
import app.config as config


class _ScriptedLLM:
    """按脚本依次产出回复，模拟 call_llm_stream 的 (kind, text) 契约。

    脚本元素：
      - str            → 作为一整块正文（"content"）
      - (kind, text)   → 原样产出，用于构造思考内容 / 分块正文
      - [(kind, text)] → 同上，一次产出多块
      - Exception      → 抛出（验证 agent 的异常兜底）
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.seen_histories = []

    def __call__(self, messages, **kwargs):
        self.calls += 1
        self.seen_histories.append(messages)
        if not self.replies:
            yield "content", "（脚本已用尽）"
            return
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, list):
            for pair in item:
                yield pair
        elif isinstance(item, tuple):
            yield item
        else:
            yield "content", item


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        # 不做上下文压缩、不做真实工具调用、不做真实网络请求
        for target, value in (
            ("trim_history", lambda h, agent_id=None: h),
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
        p = mock.patch.object(agent, "call_llm_stream", fake)
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

    def test_reasoning_is_streamed_but_never_stored(self):
        self._patch_llm([
            [("reasoning", "先看"), ("reasoning", "时间"), ("content", "现在 12 点。")],
        ])
        events = self._collect("几点了")
        self.assertEqual([e["type"] for e in events],
                         ["user", "reasoning", "reasoning", "assistant"])
        self.assertEqual("".join(e["content"] for e in events if e["type"] == "reasoning"),
                         "先看时间")
        # 思考内容"只出不进"：历史里只能有 user/assistant，且正文不含思考
        self.assertEqual([m["role"] for m in self.history], ["user", "assistant"])
        self.assertEqual(self.history[-1]["content"], "现在 12 点。")
        self.assertNotIn("先看时间", self.history[-1]["content"])

    # ─── 带图对话：图片只注入第 1 轮，且绝不进历史 ─────

    def test_image_attached_to_first_turn_only(self):
        fake = self._patch_llm(['[[TOOL:list_files]][[/TOOL]]', '看完了'])
        list(agent.run_agent_stream("（用户上传了一张图片：看图）", self.history,
                                    image="data:image/jpeg;base64,ZZZ"))
        # 第 1 轮：本轮用户消息被升级成多模态
        first = [m for m in fake.seen_histories[0] if isinstance(m.get("content"), list)]
        self.assertEqual(len(first), 1)
        parts = first[0]["content"]
        self.assertEqual(parts[0],
                         {"type": "text", "text": "（用户上传了一张图片：看图）"})
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/jpeg;base64,ZZZ")
        # 第 2 轮：不再重发图片——白烧输入 token，而且每轮都会掐断前缀缓存
        second = [m for m in fake.seen_histories[1] if isinstance(m.get("content"), list)]
        self.assertEqual(second, [])

    def test_image_never_leaks_into_history(self):
        self._patch_llm(["看图完毕。"])
        list(agent.run_agent_stream("看图", self.history,
                                    image="data:image/jpeg;base64,ZZZ"))
        # 图片本体绝不能进 history——整份历史会被原样落盘
        self.assertEqual(self.history[0], {"role": "user", "content": "看图"})

    def test_image_targets_user_message_not_tool_result(self):
        # 用户手打工具块时 pre_tool_results 排在本轮 user 之后（_history_for_llm
        # 会把它们转成 user role），倒序找必须跳过它们，否则图片挂到工具结果上
        fake = self._patch_llm(["好了"])
        list(agent.run_agent_stream(
            "看图", self.history, image="data:image/jpeg;base64,ZZZ",
            pre_tool_results=[{"name": "get_time", "result": "12:00"}]))
        multi = [m for m in fake.seen_histories[0] if isinstance(m.get("content"), list)]
        self.assertEqual(len(multi), 1)
        self.assertEqual(multi[0]["content"][0]["text"], "看图")

    def test_content_chunks_are_concatenated(self):
        self._patch_llm([[("content", "你"), ("content", "好"), ("content", "！")]])
        events = self._collect("你好")
        # 正文分块只能产生一条 assistant 事件（不是三条）
        self.assertEqual([e["type"] for e in events], ["user", "assistant"])
        self.assertEqual(events[-1]["content"], "你好！")
        self.assertEqual(self.history[-1]["content"], "你好！")

    def test_reasoning_resumes_after_tool_call(self):
        self._patch_llm([
            [("reasoning", "需要查时间"), ("content", "[[TOOL:get_time]][[/TOOL]]")],
            [("reasoning", "拿到结果了"), ("content", "现在 12:00。")],
        ])
        events = self._collect("几点了")
        self.assertEqual([e["type"] for e in events],
                         ["user", "reasoning", "tool_call", "tool_result",
                          "reasoning", "assistant"])
        self.assertEqual(events[1]["content"], "需要查时间")
        self.assertEqual(events[4]["content"], "拿到结果了")

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


class AgentToolWhitelistTest(unittest.TestCase):
    """执行层的工具白名单拦截。

    system prompt 里不列出只是「看不见」，这里保证「调不动」——
    少了这一道，「写作 agent 不能用生图」就只是名义上的隔离。
    """

    def setUp(self):
        self.history = []
        self.executed = []

        def fake_execute(name, args):
            self.executed.append(name)
            return "结果:" + name

        for target, value in (
            ("trim_history", lambda h, agent_id=None: h),
            ("execute_tool", fake_execute),
        ):
            p = mock.patch.object(agent, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(config, "MAX_TURNS", 10)
        p.start()
        self.addCleanup(p.stop)

    def _patch_llm(self, replies):
        fake = _ScriptedLLM(replies)
        p = mock.patch.object(agent, "call_llm_stream", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def _patch_whitelist(self, allowed):
        p = mock.patch.object(agent.agent_store, "allows_tool",
                              lambda aid, name: allowed)
        p.start()
        self.addCleanup(p.stop)

    def test_allowed_tool_runs(self):
        self._patch_whitelist(True)
        self._patch_llm(['[[TOOL:read_file]]{"path": "a.md"}[[/TOOL]]', '读完了。'])
        events = list(agent.run_agent_stream("读一下", self.history, agent_id="writing"))
        self.assertEqual(self.executed, ["read_file"])
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertEqual(results[0]["result"], "结果:read_file")

    def test_blocked_tool_never_executes(self):
        self._patch_whitelist(False)
        self._patch_llm(['[[TOOL:generate_image]]{"prompt": "cat"}[[/TOOL]]', '好的。'])
        events = list(agent.run_agent_stream("画只猫", self.history, agent_id="writing"))
        self.assertEqual(self.executed, [])                      # 压根没被执行
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertIn("不可用", results[0]["result"])
        # 拒绝结果同样要落进历史，模型下一轮才知道换条路
        blocked = [m for m in self.history if m.get("role") == "tool_result"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("不可用", blocked[0]["content"])


class AgentEventHistoryContractTest(unittest.TestCase):
    """契约：会话消息类事件出流时，那条消息已经写进 history。

    app/main.py 以「事件出流」作为落盘时机，所以这个顺序是不变式。
    反过来（先 yield 后 append）该条消息会赶不上落盘——客户端中断时尤其明显。
    """

    def setUp(self):
        self.history = []
        for target, value in (
            ("trim_history", lambda h, agent_id=None: h),
            ("execute_tool", lambda name, args: "工具结果:" + name),
        ):
            p = mock.patch.object(agent, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(config, "MAX_TURNS", 10)
        p.start()
        self.addCleanup(p.stop)

    def _patch_llm(self, replies):
        fake = _ScriptedLLM(replies)
        p = mock.patch.object(agent, "call_llm_stream", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def _histories_by_event(self, user_input, **kwargs):
        """手动迭代事件流，逐个记录事件出流那一刻的 history 内容。"""
        seen = {}
        gen = agent.run_agent_stream(user_input, self.history, **kwargs)
        for event in gen:
            seen.setdefault(event["type"], []).append(
                [m.get("content") for m in self.history])
        return seen

    def test_each_event_sees_its_own_message_in_history(self):
        self._patch_llm(['[[TOOL:get_time]][[/TOOL]]', '现在 12:00。'])
        seen = self._histories_by_event("几点了")

        # user 事件出流时，用户那句话已经在历史里
        self.assertEqual(seen["user"][0], ["几点了"])
        # tool_result 事件出流时，工具结果已经在历史里（原先这里是反的）
        self.assertIn("工具结果:get_time", seen["tool_result"][0])
        # assistant 事件出流时，回复正文已经在历史里
        self.assertIn("现在 12:00。", seen["assistant"][0])

    def test_mid_stream_close_keeps_emitted_messages(self):
        """客户端中途断开：已经推送出去的内容都留在 history 里。"""
        self._patch_llm(['[[TOOL:get_time]][[/TOOL]]', '现在 12:00。'])
        gen = agent.run_agent_stream("几点了", self.history)
        next(gen)                                  # user
        next(gen)                                  # tool_call（不入历史）
        gen.close()                                # 模拟断开

        contents = [m.get("content") for m in self.history]
        # 已推送的 user 在；那一轮的 assistant 原文（工具块）也在，
        # 因为它在解析工具调用之前就已入历史——只是没有单独出流。
        self.assertEqual(contents[0], "几点了")
        self.assertNotIn("现在 12:00。", contents)   # 还没生成的当然没有


if __name__ == "__main__":
    unittest.main()
