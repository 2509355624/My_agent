# -*- coding: utf-8 -*-
"""会话压缩（memory.trim_window）与其配套链路的测试。

2026-09-29 起判据由「轮数」改成「token 预算」：到 CONTEXT_BUDGET（默认 5 万）
就压。原来按 CONTEXT_MAX_TURNS=100 轮开窗，实测群会话能攒到 8 万 token
（534 条 / 16.6 万字），单轮又慢又贵。

四件事：
1. 压缩：群会话超出预算时只留装得下的最近若干轮，更老的转交 longterm
   （mock，不真调 API）；
2. 去重与攒批：save_history 一次回复内会被调用多次，同一批滚出窗口的
   消息只允许摘要一次；滚出消息攒够 _DIGEST_BATCH_MIN 才摘一次，
   不然记忆库全是 2 条消息的碎片；
3. 回退：非群会话（网页 / 私聊）没有记忆库，走 trim_history 老路，且
   **必须原地收缩**调用方手上的 list（压缩不幂等，见 trim_window 注释）；
4. 另外覆盖 longterm.digest_messages_async（窗口摘要落盘）与 web_search
   的结果截断（tool_result 永久占历史，必须掐）。

token 估算口径见 memory.estimate_tokens：汉字 ×0.6 + 其他 ×0.3，每条消息
另加 4 的角色开销。下面的 _body/_turn 就是按这个口径造的「稳定尺寸砖块」。
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


# ── 造数据：每轮固定 246 token，方便把预算算清楚 ────────────────
# _CHARS 个汉字（末尾换成序号保证内容唯一——去重指纹靠 (role, content)，
# 内容全一样的话第二批消息会被当成重复全部吃掉）。
_CHARS = 200


def _body(i):
    """第 i 轮的正文：唯一、且长度稳定（序号占 1~2 字符，估算是定值）。"""
    return "字" * (_CHARS - len(str(i))) + str(i)


def _turn(i):
    """一轮 = user + assistant，各 123 token（119 内容 + 4 开销）。"""
    return [{"role": "user", "content": _body(i)},
            {"role": "assistant", "content": _body(i)}]


def _history(turns, system=True):
    h = []
    if system:
        h.append({"role": "system", "content": "人设"})     # 5 token
    for i in range(turns):
        h.extend(_turn(i))
    return h


# 5(系统) + 246×12 = 2957 ≤ 3000 < 3203 = 246×13
_BUDGET = 3000
_KEEP = 12


class _ResetState:
    """清掉模块级去重/攒批状态，测试之间互不污染。"""

    def setUp(self):
        super().setUp()
        memory._digest_seen.clear()
        memory._digest_pending.clear()
        self.addCleanup(memory._digest_seen.clear)
        self.addCleanup(memory._digest_pending.clear)


class TrimWindowTest(_ResetState, unittest.TestCase):
    def test_turn_estimate_is_stable(self):
        """钉住估算口径：下面的轮数/预算换算全建立在这个前提上。"""
        self.assertEqual(memory.estimate_messages(_turn(3)), 246)
        self.assertEqual(memory.estimate_messages(_turn(99)), 246)
        self.assertEqual(memory.estimate_messages(_history(0)), 5)

    def test_over_budget_keeps_recent_turns(self):
        # 35 轮 ≈ 8615 token，远超 3000；从最新往回装，装下 12 轮，滚掉 23 轮。
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(_history(35), "qq", "group_9",
                                     budget=_BUDGET)
        roles = [m["role"] for m in out]
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles.count("user"), _KEEP)      # 只剩最近 12 轮
        self.assertEqual(out[1]["content"], _body(23))    # 前 23 轮被摘走
        self.assertEqual(out[-1]["content"], _body(34))
        dig.assert_called_once()
        args = dig.call_args[0]
        self.assertEqual(args[0], "qq")
        self.assertEqual(args[1], "9")
        self.assertEqual(len(args[2]), 46)                # 23 轮 = 46 条消息

    def test_result_fits_the_budget(self):
        """压完必须真的装得进预算——这是「最坏情况花多少钱」的保证。"""
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async"):
            out = memory.trim_window(_history(200), "qq", "group_9",
                                     budget=_BUDGET)
        self.assertLessEqual(memory.estimate_messages(out), _BUDGET)

    def test_within_budget_untouched(self):
        # 12 轮 = 2957 < 3000：原样返回，连对象都不换（保住热前缀）
        h = _history(12)
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        self.assertIs(out, h)
        self.assertEqual(len(out), 25)
        dig.assert_not_called()

    def test_just_over_budget_trims_the_oldest_turn(self):
        # 13 轮 = 3203 > 3000：只滚掉最老的一轮
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(_history(13), "qq", "group_9",
                                     budget=_BUDGET)
        self.assertEqual(len(out), 25)
        self.assertEqual(out[1]["content"], _body(1))
        self.assertEqual(len(dig.call_args[0][2]), 2)

    def test_last_turn_always_kept(self):
        """单轮就超预算也得留一轮，否则窗口空掉、模型看不见刚说的话。"""
        h = [{"role": "system", "content": "人设"}] + _turn(0)
        h[-1] = {"role": "assistant", "content": "字" * 20000}
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async"):
            out = memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        self.assertEqual(out[-1]["content"], "字" * 20000)

    def test_zero_disables(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(_history(35), "qq", "group_9", max_turns=0)
        self.assertEqual(len(out), 71)
        dig.assert_not_called()

    def test_private_session_falls_back_to_trim_history(self):
        """私聊 / 网页没有记忆库：不摘要、走 trim_history 的 token 预算老路。"""
        with mock.patch.object(longterm, "digest_messages_async") as dig, \
             mock.patch.object(memory, "trim_history",
                               return_value=[{"role": "user", "content": "旧路"}]) as th:
            out = memory.trim_window(_history(35), "qq", "user_123",
                                     budget=_BUDGET)
        dig.assert_not_called()
        th.assert_called_once()
        self.assertEqual(out, [{"role": "user", "content": "旧路"}])
        # 私聊不给 hit_rate（线程本地 usage 在 QQ 跨线程下恒为 0），给 0 即「到线就压」
        self.assertEqual(th.call_args[1]["usage"]["hit_rate"], 0.0)
        self.assertEqual(th.call_args[1]["budget"], _BUDGET)

    def test_web_session_without_key_falls_back(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig, \
             mock.patch.object(memory, "trim_history", return_value=[]) as th:
            memory.trim_window(_history(35), "main", None, budget=_BUDGET)
        dig.assert_not_called()
        th.assert_called_once()

    def test_digest_failure_does_not_break_window(self):
        """longterm 起不来线程也不影响压缩——历史不能因为后台失败继续膨胀。"""
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async",
                               side_effect=OSError("boom")):
            out = memory.trim_window(_history(35), "qq", "group_9",
                                     budget=_BUDGET)
        self.assertEqual(out[1]["content"], _body(23))


class InPlaceShrinkTest(_ResetState, unittest.TestCase):
    """非群会话压缩后必须**原地收缩**调用方手上的 list。

    压缩不幂等：save_history 一轮里被调很多次，副本式裁剪下第一次写了瘦文件、
    水位也记上了，同轮后续保存又被冷却挡住 → 把胖历史原样写回，压缩成果被
    冲掉（实测私聊压完 547 条回弹）。
    """

    def test_private_trim_shrinks_caller_list(self):
        h = _history(35)
        with mock.patch.object(memory, "trim_history",
                               return_value=[{"role": "user", "content": "摘要"}]):
            memory.trim_window(h, "qq", "user_123", budget=_BUDGET)
        self.assertEqual(h, [{"role": "user", "content": "摘要"}])

    def test_unchanged_result_keeps_the_same_object(self):
        h = _history(35)
        with mock.patch.object(memory, "trim_history", return_value=h):
            out = memory.trim_window(h, "qq", "user_123", budget=_BUDGET)
        self.assertIs(out, h)


class DigestBatchTest(_ResetState, unittest.TestCase):
    """滚出消息攒批：攒够阈值才摘要一次，避免碎片记忆。"""

    def test_below_threshold_buffers_without_digest(self):
        # 14 轮滚掉 2 轮 = 4 条 < 40，先攒着
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(14), "qq", "group_9", budget=_BUDGET)
        dig.assert_not_called()
        self.assertEqual(len(memory._digest_pending[("qq", "9")]), 4)

    def test_crossing_threshold_flushes_whole_batch(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(14), "qq", "group_9", budget=_BUDGET)
            # 再滚 46 条，其中 4 条与上批重合被去重，攒批总数 46 → 触发
            memory.trim_window(_history(35), "qq", "group_9", budget=_BUDGET)
        dig.assert_called_once()
        batch = dig.call_args[0][2]
        self.assertEqual(len(batch), 46)                 # 4 + 42（去重后），一次整批
        self.assertEqual(batch[0]["content"], _body(0))  # 顺序保持旧→新
        self.assertEqual(batch[-1]["content"], _body(22))
        self.assertEqual(memory._digest_pending.get(("qq", "9")), [])

    def test_big_initial_roll_flushes_immediately(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(60), "qq", "group_9", budget=_BUDGET)
        dig.assert_called_once()
        self.assertEqual(len(dig.call_args[0][2]), 96)   # 48 轮 = 96 条

    def test_groups_buffered_separately(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(14), "qq", "group_9", budget=_BUDGET)
            memory.trim_window(_history(14), "qq", "group_8", budget=_BUDGET)
        dig.assert_not_called()
        self.assertEqual(len(memory._digest_pending[("qq", "9")]), 4)
        self.assertEqual(len(memory._digest_pending[("qq", "8")]), 4)


class DigestDedupTest(_ResetState, unittest.TestCase):
    """save_history 一次回复内多次落盘，同一批消息只许摘要一次。"""

    def test_same_batch_digests_once(self):
        h = _history(35)
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        dig.assert_called_once()
        self.assertEqual(dig.call_args[0][2][0]["content"], _body(0))

    def test_growth_inside_last_turn_only_digests_fresh(self):
        """最后一轮在轮尾继续长（assistant/tool 续在后面），不重复摘要。"""
        h = _history(35)
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
            self.assertEqual(dig.call_count, 1)
            h.extend([{"role": "assistant", "content": "答1"},
                      {"role": "assistant", "content": "答2"}])   # 轮尾继续长
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        # 轮尾变长只会把更老的轮挤出去；已摘过的消息不重摘
        self.assertGreaterEqual(dig.call_count, 1)
        self.assertEqual(dig.call_args[0][2][0]["content"], _body(0))
        self.assertNotIn("答1", [m["content"] for m in dig.call_args[0][2]])

    def test_slide_digests_only_newly_rolled(self):
        """预算滑动：新一轮把更老的挤出去时，只摘新滚出的，不重摘旧的。"""
        h = _history(13)                      # 13 轮 → 滚 turn0
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
            h.extend(_turn(13))               # 14 轮 → 滚 turn0..1
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        self.assertEqual(dig.call_count, 2)
        self.assertEqual(dig.call_args[0][2], _turn(1))


class SaveHistoryIntegrationTest(unittest.TestCase):
    """save_history 落盘前真的会压缩——磁盘上的历史不再无限膨胀。"""

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

    def test_saved_file_stays_within_budget(self):
        # 显式钉住预算：不依赖 .env 的 CONTEXT_BUDGET
        h = _history(35)
        with mock.patch.object(memory, "CONTEXT_BUDGET", _BUDGET), \
             mock.patch.object(longterm, "digest_messages_async"):
            memory.save_history(h, "qq", "group_9")
        loaded = memory.load_history("qq", "group_9")
        self.assertEqual([m["role"] for m in loaded].count("user"), _KEEP)
        self.assertEqual(loaded[1]["content"], _body(23))
        self.assertLessEqual(memory.estimate_messages(loaded), _BUDGET)


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
