# -*- coding: utf-8 -*-
"""轮数开窗（memory.trim_window）与其配套链路的测试。

三件事：
1. 开窗：群会话超过 CONTEXT_MAX_TURNS 轮时只留最近 N 轮，滚出去的转交
   longterm（mock，不真调 API）；
2. 去重与攒批：save_history 一次回复内会被调用多次，同一批滚出窗口的
   消息只允许摘要一次；滚出消息攒够 _DIGEST_BATCH_MIN 才摘一次，
   不然记忆库全是 2 条消息的碎片；
3. 回退：非群会话（网页 / 私聊）没有记忆库，走 trim_history 老路。
另外覆盖 longterm.digest_messages_async（窗口摘要落盘）与
web_search 的结果截断（tool_result 永久占历史，必须掐）。
"""

import os
import tempfile
import threading
import unittest
from unittest import mock

import app.agents as agents
import app.memory as memory
import app.longterm as longterm
from app.tools.normal import web_search


def _turn(i):
    """一轮 = user + assistant。"""
    return [{"role": "user", "content": "u%d" % i},
            {"role": "assistant", "content": "a%d" % i}]


def _history(turns, system=True):
    h = []
    if system:
        h.append({"role": "system", "content": "人设"})
    for i in range(turns):
        h.extend(_turn(i))
    return h


class _ResetState:
    """清掉模块级去重/攒批状态，测试之间互不污染。"""

    def setUp(self):
        super().setUp()
        memory._digest_seen.clear()
        memory._digest_pending.clear()
        self.addCleanup(memory._digest_seen.clear)
        self.addCleanup(memory._digest_pending.clear)


class TrimWindowTest(_ResetState, unittest.TestCase):
    def test_over_window_keeps_recent_turns(self):
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(_history(25), "qq", "group_9", max_turns=20)
        roles = [m["role"] for m in out]
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles.count("user"), 20)       # 只剩最近 20 轮
        self.assertEqual(out[1]["content"], "u5")       # 前 5 轮被摘走
        self.assertEqual(out[-1]["content"], "a24")
        dig.assert_called_once()
        args = dig.call_args[0]
        self.assertEqual(args[0], "qq")
        self.assertEqual(args[1], "9")
        self.assertEqual(len(args[2]), 10)              # 5 轮 = 10 条消息

    def test_within_window_untouched(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(_history(15), "qq", "group_9", max_turns=20)
        self.assertEqual(len(out), 31)
        dig.assert_not_called()

    def test_zero_disables(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(_history(30), "qq", "group_9", max_turns=0)
        self.assertEqual(len(out), 61)
        dig.assert_not_called()

    def test_private_session_falls_back_to_trim_history(self):
        """私聊 / 网页没有记忆库：不摘要、走 token 预算老路。"""
        with mock.patch.object(longterm, "digest_messages_async") as dig, \
             mock.patch.object(memory, "trim_history",
                               return_value=[{"role": "user", "content": "旧路"}]) as th:
            out = memory.trim_window(_history(30), "qq", "user_123", max_turns=20)
        dig.assert_not_called()
        th.assert_called_once()
        self.assertEqual(out, [{"role": "user", "content": "旧路"}])

    def test_web_session_without_key_falls_back(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig, \
             mock.patch.object(memory, "trim_history", return_value=[]) as th:
            memory.trim_window(_history(30), "main", None, max_turns=20)
        dig.assert_not_called()
        th.assert_called_once()

    def test_digest_failure_does_not_break_window(self):
        """longterm 起不来线程也不影响开窗——历史不能因为后台失败继续膨胀。"""
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async",
                               side_effect=OSError("boom")):
            out = memory.trim_window(_history(25), "qq", "group_9", max_turns=20)
        self.assertEqual([m["content"] for m in out][1], "u5")


class DigestBatchTest(_ResetState, unittest.TestCase):
    """滚出消息攒批：攒够阈值才摘要一次，避免碎片记忆。"""

    def test_below_threshold_buffers_without_digest(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(25), "qq", "group_9", max_turns=20)
        dig.assert_not_called()                          # 10 条 < 40，先攒着
        self.assertEqual(len(memory._digest_pending[("qq", "9")]), 10)

    def test_crossing_threshold_flushes_whole_batch(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(25), "qq", "group_9", max_turns=20)
            # 再滚 30 轮 = 60 条，其中 10 条与上批重合被去重，攒批总数 60 → 触发
            memory.trim_window(_history(50), "qq", "group_9", max_turns=20)
        dig.assert_called_once()
        batch = dig.call_args[0][2]
        self.assertEqual(len(batch), 60)                 # 10 + 50（去重后），一次整批
        self.assertEqual(batch[0]["content"], "u0")      # 顺序保持旧→新
        self.assertEqual(batch[-1]["content"], "a29")
        self.assertEqual(memory._digest_pending.get(("qq", "9")), [])

    def test_big_initial_roll_flushes_immediately(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(60), "qq", "group_9", max_turns=20)
        dig.assert_called_once()
        self.assertEqual(len(dig.call_args[0][2]), 80)   # 40 轮 = 80 条

    def test_groups_buffered_separately(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(25), "qq", "group_9", max_turns=20)
            memory.trim_window(_history(25), "qq", "group_8", max_turns=20)
        dig.assert_not_called()
        self.assertEqual(len(memory._digest_pending[("qq", "9")]), 10)
        self.assertEqual(len(memory._digest_pending[("qq", "8")]), 10)


class DigestDedupTest(_ResetState, unittest.TestCase):
    """save_history 一次回复内多次落盘，同一批消息只许摘要一次。"""

    def test_same_batch_digests_once(self):
        h = _history(25)
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(h, "qq", "group_9", max_turns=20)
            memory.trim_window(h, "qq", "group_9", max_turns=20)
            memory.trim_window(h, "qq", "group_9", max_turns=20)
        dig.assert_called_once()

    def test_growth_inside_last_turn_only_digests_fresh(self):
        """同一回复内最后一轮继续长（assistant/tool 续在轮尾），不再重复摘要。"""
        h = _history(20)                     # 正好 20 轮
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            h.append({"role": "user", "content": "新问题"})       # 第 21 轮开始
            memory.trim_window(h, "qq", "group_9", max_turns=20)
            self.assertEqual(dig.call_count, 1)  # 最老一轮被挤出去
            h.extend([{"role": "assistant", "content": "答1"},
                      {"role": "assistant", "content": "答2"}])  # 轮尾继续长
            memory.trim_window(h, "qq", "group_9", max_turns=20)
            memory.trim_window(h, "qq", "group_9", max_turns=20)
        self.assertEqual(dig.call_count, 1)  # 没有新轮次滚出，不再摘要
        first = dig.call_args[0][2]
        self.assertEqual(first, [{"role": "user", "content": "u0"},
                                 {"role": "assistant", "content": "a0"}])

    def test_window_slide_digests_only_newly_rolled(self):
        """窗口滑动：新一轮把更老的挤出去时，只摘新滚出的，不重摘旧的。"""
        h = _history(20)
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            h.append({"role": "user", "content": "新问题"})       # 21 轮，挤掉 t0
            memory.trim_window(h, "qq", "group_9", max_turns=20)
            h.extend(_turn(100))                                  # 22 轮，挤掉 t1
            memory.trim_window(h, "qq", "group_9", max_turns=20)
        self.assertEqual(dig.call_count, 2)
        self.assertEqual(dig.call_args[0][2],
                         [{"role": "user", "content": "u1"},
                          {"role": "assistant", "content": "a1"}])


class SaveHistoryIntegrationTest(unittest.TestCase):
    """save_history 落盘前真的会开窗——磁盘上的历史不再无限膨胀。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(agents, "AGENTS_DIR",
                              os.path.join(self.tmp.name, "agents"))
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        memory._digest_seen.clear()
        memory._digest_pending.clear()
        self.addCleanup(memory._digest_seen.clear)
        self.addCleanup(memory._digest_pending.clear)

    def test_saved_file_stays_within_window(self):
        h = _history(30)
        with mock.patch.object(longterm, "digest_messages_async"):
            memory.save_history(h, "qq", "group_9")
        loaded = memory.load_history("qq", "group_9")
        self.assertEqual([m["role"] for m in loaded].count("user"), 20)
        self.assertEqual(loaded[1]["content"], "u10")


class GroupKeyTest(unittest.TestCase):
    def test_group_prefix_stripped(self):
        self.assertEqual(memory._group_id_from_key("group_885143276"), "885143276")

    def test_private_and_empty_return_blank(self):
        self.assertEqual(memory._group_id_from_key("user_123"), "")
        self.assertEqual(memory._group_id_from_key(None), "")


class DigestMessagesAsyncTest(unittest.TestCase):
    """longterm.digest_messages_async：窗口消息 → 150 字记忆落盘。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(agents, "AGENTS_DIR",
                              os.path.join(self.tmp.name, "agents"))
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)

    def _run(self, msgs, llm_return="摘要内容"):
        with mock.patch.object(longterm, "call_llm", return_value=llm_return) as llm:
            longterm.digest_messages_async("qq", "9", msgs)
            # digest_messages_async 起后台线程；同步等它收尾再断言
            for t in threading.enumerate():
                if t.name == "memory-digest":
                    t.join(timeout=5)
        return llm

    def test_writes_short_record(self):
        msgs = [{"role": "user", "content": "小明：帮我看看报错"},
                {"role": "assistant", "content": "发来"}]
        llm = self._run(msgs)
        sys_content = llm.call_args[0][0][0]["content"]
        self.assertIn("150 字", sys_content)        # 用的是短摘要口径
        path = longterm._path("qq", "9")
        self.assertTrue(os.path.exists(path))
        import json
        rec = json.loads(open(path, encoding="utf-8").readlines()[-1])
        self.assertEqual(rec["s"], "摘要内容")
        self.assertEqual(rec["n"], 2)

    def test_skips_system_and_empty(self):
        msgs = [{"role": "system", "content": "人设"},
                {"role": "user", "content": "  "}]
        llm = self._run(msgs)
        llm.assert_not_called()

    def test_failure_writes_nothing(self):
        with mock.patch.object(longterm, "call_llm",
                               side_effect=RuntimeError("挂了")):
            longterm.digest_messages_async("qq", "9",
                                           [{"role": "user", "content": "hi"}])
            for t in threading.enumerate():
                if t.name == "memory-digest":
                    t.join(timeout=5)
        self.assertFalse(os.path.exists(longterm._path("qq", "9")))

    def test_body_capped_from_the_end(self):
        long_msgs = [{"role": "user", "content": "字" * 6000} for _ in range(3)]
        llm = self._run(long_msgs)
        body = llm.call_args[0][0][1]["content"]
        self.assertLessEqual(len(body), longterm._SOURCE_MAX_CHARS + 20)


class WebSearchTruncateTest(unittest.TestCase):
    def test_long_result_truncated(self):
        text = "x" * 3000
        out = web_search._truncate(text)
        self.assertLess(len(out), 1000)
        self.assertIn("截断", out)

    def test_short_result_untouched(self):
        self.assertEqual(web_search._truncate("短结果"), "短结果")

    def test_zero_disables(self):
        with mock.patch.object(web_search, "WEB_SEARCH_MAX_CHARS", 0):
            self.assertEqual(web_search._truncate("x" * 3000), "x" * 3000)

    def test_web_search_pipeline_truncates(self):
        with mock.patch.object(web_search, "_doubao_search",
                               return_value="y" * 3000):
            out = web_search._web_search("测试")
        self.assertLess(len(out), 1000)
