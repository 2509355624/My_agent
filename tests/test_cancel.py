# -*- coding: utf-8 -*-
"""手动中断（app/cancel.py + agent 循环的三个检查点）测试。

用户点「停止」后能不能真的停下来，取决于四件事，逐条钉住：
1. 取消事件按 request_id 隔离、收尾时摘除（否则注册表随会话一直变大）；
2. 没有事件时一切照旧——不带 request_id 的调用不该被拉进取消链路；
3. 循环在三个检查点都看得到它：轮次边界、模型输出后、工具执行前；
4. 收尾是"正常结束"——落盘 + 出 aborted 事件，不抛异常也不强杀进程。

真机（浏览器里点停止）另做验证，这里只管逻辑。
"""

import threading
import unittest
from unittest import mock

import app.agent as agent
import app.cancel as cancel
import app.config as config
import app.llm as llm


class _ScriptedLLM:
    """按脚本依次产出回复，记录被调用了几次（中断的核心断言就是"少调几次"）。"""

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


class CancelRegistryTest(unittest.TestCase):
    def tearDown(self):
        # 模块级全局状态：每个用例都清干净，免得相互影响
        for rid in ("r1", "r2"):
            cancel.unregister(rid)
        cancel.bind(None)

    def test_register_returns_fresh_event(self):
        ev = cancel.register("r1")
        self.assertIsInstance(ev, threading.Event)
        self.assertFalse(ev.is_set())

    def test_empty_request_id_gets_no_event(self):
        # 空 id 不能建键：否则所有不带 id 的请求会共用 None 这个键，互相误伤
        self.assertIsNone(cancel.register(""))
        self.assertIsNone(cancel.register(None))

    def test_cancel_unknown_id_reports_miss(self):
        self.assertFalse(cancel.cancel("r1"))   # 没登记过
        self.assertFalse(cancel.cancel(""))

    def test_unregister_drops_the_key(self):
        cancel.register("r1")
        self.assertEqual(cancel.active_count(), 1)
        cancel.unregister("r1")
        self.assertEqual(cancel.active_count(), 0)
        self.assertFalse(cancel.cancel("r1"))

    def test_is_cancelled_without_event_is_false(self):
        # 没有事件 = 没人要求中断
        self.assertFalse(cancel.is_cancelled(None))
        cancel.bind(None)
        self.assertFalse(cancel.is_cancelled())

    def test_is_cancelled_reads_bound_event(self):
        ev = cancel.register("r1")
        cancel.bind(ev)
        self.assertFalse(cancel.is_cancelled())
        cancel.cancel("r1")
        self.assertTrue(cancel.is_cancelled())

    def test_thread_local_is_isolated(self):
        # 工具层靠线程本地变量取事件：一个请求线程的中断不能传染给另一个
        ev = cancel.register("r1")
        cancel.cancel("r1")
        seen = {}

        def worker():
            seen["cancelled"] = cancel.is_cancelled()

        cancel.bind(ev)
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertFalse(seen["cancelled"])


class AgentCancelTest(unittest.TestCase):
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

    def _run(self, llm_fn, history, cancel_event=None):
        with mock.patch.object(agent, "call_llm_stream", llm_fn):
            return list(agent.run_agent_stream("你好", history,
                                               cancel_event=cancel_event))

    # ─── 检查点①：轮次边界 ───────────────────────────

    def test_pre_cancelled_stops_before_any_llm_call(self):
        ev = threading.Event()
        ev.set()
        fake = _ScriptedLLM(["不该被调用"])
        history = []
        events = self._run(fake, history, ev)

        self.assertEqual(fake.calls, 0)          # 一次模型调用都没发生
        self.assertEqual(events[-1]["type"], "aborted")
        # 用户消息照常入历史（契约：事件出流时那条消息已在 history 里）
        self.assertEqual([m["role"] for m in history][0], "user")

    def test_abort_during_tool_execution_skips_next_turn(self):
        # 工具执行期间按停止 → 工具结果照常落盘，但不再进入下一轮
        ev = threading.Event()

        def execute_and_cancel(name, args):
            ev.set()
            return "工具结果:" + name

        fake = _ScriptedLLM(['[[TOOL:get_time]]{}', "第二轮不该跑"])
        history = []
        with mock.patch.object(agent, "execute_tool", execute_and_cancel):
            with mock.patch.object(agent, "call_llm_stream", fake):
                events = list(agent.run_agent_stream("你好", history,
                                                     cancel_event=ev))

        self.assertEqual(fake.calls, 1)          # 只跑了第 1 轮
        self.assertEqual(events[-1]["type"], "aborted")
        self.assertTrue(any(m.get("tool_name") == "get_time"
                            for m in history if m.get("role") == "tool_result"))

    # ─── 检查点②：模型输出结束 ───────────────────────

    def test_abort_after_model_output_keeps_partial_reply(self):
        ev = threading.Event()

        def streaming(messages, **kwargs):
            yield "content", "说到一半"
            ev.set()                              # 用户此刻按了停止

        history = []
        with mock.patch.object(agent, "call_llm_stream", streaming):
            events = list(agent.run_agent_stream("你好", history,
                                                 cancel_event=ev))

        assistants = [m for m in history if m["role"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["content"], "说到一半")   # 已收到的都留着
        self.assertEqual(events[-1]["type"], "aborted")

    def test_abort_does_not_execute_tool_from_truncated_reply(self):
        # 这是检查点②存在的真正理由：半截回复里的 [[TOOL:...]] 参数是残缺的，
        # 花括号配对能"成功"解析出一个不完整参数，真执行了就是拿垃圾参数去生图。
        ev = threading.Event()
        executed = []

        def spy_execute(name, args):
            executed.append((name, args))
            return "不该执行"

        def streaming(messages, **kwargs):
            yield "content", '这就来做\n[[TOOL:generate_image]]{"prompt": "a cat'
            ev.set()

        history = []
        with mock.patch.object(agent, "execute_tool", spy_execute):
            with mock.patch.object(agent, "call_llm_stream", streaming):
                events = list(agent.run_agent_stream("画张图", history,
                                                     cancel_event=ev))

        self.assertEqual(executed, [])           # 一个工具都没执行
        self.assertEqual(events[-1]["type"], "aborted")

    # ─── 检查点③：工具执行前 ─────────────────────────

    def test_abort_before_tool_skips_execution(self):
        ev = threading.Event()
        executed = []

        def execute_and_cancel_on_first(name, args):
            executed.append(name)
            ev.set()          # 第一个工具执行期间用户按了停止
            return "结果"

        def streaming(messages, **kwargs):
            yield "content", '[[TOOL:t1]]{}[[TOOL:t2]]{}'

        with mock.patch.object(agent.agent_store, "allows_tool",
                               lambda aid, name: True):
            with mock.patch.object(agent, "execute_tool",
                                   execute_and_cancel_on_first):
                with mock.patch.object(agent, "call_llm_stream", streaming):
                    events = list(agent.run_agent_stream("跑两个工具", [],
                                                         cancel_event=ev))

        # 第一个工具执行时置位 → 第二个被检查点③拦住，不会再执行
        self.assertEqual(executed, ["t1"])
        self.assertEqual(events[-1]["type"], "aborted")

    # ─── 收尾与兼容 ─────────────────────────────────

    def test_abort_note_is_written_to_history_and_event(self):
        ev = threading.Event()
        ev.set()
        history = []
        events = self._run(_ScriptedLLM([]), history, ev)

        notes = [m for m in history if m.get("role") == "tool_result"
                 and m.get("tool_name") == "user_cancel"]
        self.assertEqual(len(notes), 1)
        self.assertIn("中断", notes[0]["content"])
        self.assertIn("中断", events[-1]["content"])

    def test_no_event_means_normal_run(self):
        # 回归：不带 cancel_event 时行为与从前完全一致，事件流里没有 aborted
        fake = _ScriptedLLM(["直接回答"])
        history = []
        events = self._run(fake, history)

        self.assertNotIn("aborted", [e["type"] for e in events])
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])


class _FakeResp:
    """伪造 requests 的流式响应，够 llm.call_llm_stream 用就行。"""

    def __init__(self, frames):
        self.status_code = 200
        self._frames = frames
        self.closed = False
        self.elapsed = 0

    def iter_content(self, chunk_size=None):
        for f in self._frames:
            yield (f + "\n").encode("utf-8")

    def close(self):
        self.closed = True


class LlmStreamCancelTest(unittest.TestCase):
    def test_cancel_stops_reading_and_closes_upstream(self):
        # 这是"点停止能省钱"的那一处：断开上游连接，剩下的 token 不再生成
        ev = threading.Event()
        posts = []
        eff = {"provider": "deepseek", "base_url": "https://example.invalid",
               "model": "m", "api_key": "k"}

        def fake_post(url, **kwargs):
            resp = _FakeResp([
                'data: {"choices":[{"delta":{"content":"A"}}]}',
                'data: {"choices":[{"delta":{"content":"B"}}]}',
                'data: {"choices":[{"delta":{"content":"C"}}]}',
            ])
            posts.append(resp)
            return resp

        with mock.patch.object(llm, "get_effective_config", lambda p, m: eff), \
                mock.patch.object(llm.requests, "post", fake_post):
            got = []
            for _kind, text in llm.call_llm_stream(
                    [{"role": "user", "content": "x"}],
                    provider="deepseek", cancel_event=ev):
                got.append(text)
                if text == "A":
                    ev.set()                     # 收到第一块后用户按停止

        self.assertEqual(got, ["A"])             # 后两块没读
        self.assertTrue(posts[0].closed)         # 上游连接已关

    def test_without_cancel_event_reads_everything(self):
        eff = {"provider": "deepseek", "base_url": "https://example.invalid",
               "model": "m", "api_key": "k"}

        def fake_post(url, **kwargs):
            return _FakeResp([
                'data: {"choices":[{"delta":{"content":"A"}}]}',
                'data: {"choices":[{"delta":{"content":"B"}}]}',
            ])

        with mock.patch.object(llm, "get_effective_config", lambda p, m: eff), \
                mock.patch.object(llm.requests, "post", fake_post):
            got = [t for _k, t in llm.call_llm_stream(
                [{"role": "user", "content": "x"}], provider="deepseek")]

        self.assertEqual(got, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
