# -*- coding: utf-8 -*-
"""群聊长期记忆测试（app/longterm.py + recent 裁剪触发点）。

长期记忆 = 归档时把丢掉的 200 条消息压成一条摘要，按群存、回复时注入。
这里守四件事：

1. 摘要真的落盘（格式、字段、按群隔离、非法群号不写）；
2. 摘要失败绝不抛错、绝不写半截（原文在 archive 里，本轮没记忆就没了）；
3. 注入渲染的字数闸/条数闸/顺序（与 format_recent 同一个取舍：近的比远的要紧）；
4. recent._trim 真的会触发摘要（这是唯一的触发点，漏了功能就是死的）。

摘要调用是后台线程——测试里用「同步假线程」让它变确定性，不靠 sleep 等。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.longterm as longterm
import app.recent as recent


class _TmpAgentsMixin:
    """把 AGENTS_DIR 指到临时目录，别碰真实数据。"""

    def _setup_tmp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        os.makedirs(os.path.join(self.root, "qq"), exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)


def _line(name, text, user_id="1"):
    return json.dumps({"t": 1, "u": user_id, "n": name, "x": text},
                      ensure_ascii=False)


def _mem_path(root, group="9"):
    return os.path.join(root, "qq", "memory", "group_%s.jsonl" % group)


# ─── 摘要落盘 ───────────────────────────────────────

class DigestTest(_TmpAgentsMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tmp()

    def test_digest_writes_record(self):
        with mock.patch.object(longterm, "call_llm", return_value="他们聊了画图。"):
            longterm._digest("qq", "9",
                             [{"t": 1, "u": "1", "n": "甲", "x": "画个猫"}])
        with open(_mem_path(self.root), "r", encoding="utf-8") as f:
            recs = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["s"], "他们聊了画图。")
        self.assertEqual(recs[0]["n"], 1)
        self.assertEqual(recs[0]["d"].count("-"), 2)        # 日期字段在
        self.assertIsInstance(recs[0]["t"], int)

    def test_digest_failure_writes_nothing(self):
        """模型调用失败 = 本轮没记忆，但绝不能抛错（线程里抛了没人接）。"""
        with mock.patch.object(longterm, "call_llm",
                               side_effect=RuntimeError("boom")):
            longterm._digest("qq", "9", [{"t": 1, "u": "1", "n": "甲", "x": "hi"}])
        self.assertFalse(os.path.exists(_mem_path(self.root)))

    def test_digest_empty_summary_writes_nothing(self):
        with mock.patch.object(longterm, "call_llm", return_value="  \n"):
            longterm._digest("qq", "9", [{"t": 1, "u": "1", "n": "甲", "x": "hi"}])
        self.assertFalse(os.path.exists(_mem_path(self.root)))

    def test_digest_invalid_group_writes_nothing(self):
        with mock.patch.object(longterm, "call_llm", return_value="内容"):
            longterm._digest("qq", "../evil", [{"t": 1, "u": "1", "n": "甲", "x": "hi"}])
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "qq", "memory")))

    def test_digest_passes_agent_provider_and_model(self):
        """provider/model 必须显式传——漏传会回退全局默认烧错家的额度。"""
        with mock.patch.object(longterm, "call_llm", return_value="ok") as llm, \
             mock.patch.object(longterm.agent_store, "agent_config",
                               return_value={"provider": "deepseek",
                                             "model": "deepseek-flash"}):
            longterm._digest("qq", "9", [{"t": 1, "u": "1", "n": "甲", "x": "hi"}])
        kwargs = llm.call_args[1]
        self.assertEqual(kwargs.get("provider"), "deepseek")
        self.assertEqual(kwargs.get("model"), "deepseek-flash")

    def test_digest_source_capped_from_the_end(self):
        """原文超长时从后往前留——近的比远的要紧。

        每条带唯一标记，才能断言「最新的留、最旧的丢」（一样长的文本
        子串检查是没有区分度的）。
        """
        recs = [{"t": 1, "u": "1", "n": "甲", "x": "标记%03d" % i + "x" * 100}
                for i in range(200)]
        with mock.patch.object(longterm, "call_llm", return_value="ok") as llm:
            longterm._digest("qq", "9", recs)
        content = llm.call_args[0][0][1]["content"]
        self.assertLessEqual(len(content), len("聊天记录：\n") + 8000)
        self.assertIn("标记199", content)                   # 最新的在
        self.assertNotIn("标记000", content)                # 最旧的被丢


# ─── 入口：解析 + 后台线程 ──────────────────────────

class _SyncThread:
    """把 Thread 换成「同步执行 target」的假货，让摘要变确定性。"""

    def __init__(self, target=None, args=(), daemon=False, name=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class DigestAsyncTest(_TmpAgentsMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tmp()

    def _run_async(self, lines, group="9"):
        with mock.patch.object(longterm.threading, "Thread", _SyncThread), \
             mock.patch.object(longterm, "call_llm", return_value="回忆"):
            longterm.digest_async("qq", group, lines)

    def test_async_writes_summary(self):
        self._run_async([_line("甲", "m1"), _line("甲", "m2")])
        self.assertTrue(os.path.exists(_mem_path(self.root)))

    def test_async_skips_broken_lines(self):
        self._run_async(["{bad json", "", _line("甲", "m1"), "null"])
        with open(_mem_path(self.root), "r", encoding="utf-8") as f:
            recs = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(recs[0]["n"], 1)                   # 只有 1 条有效

    def test_async_no_valid_lines_no_thread_no_file(self):
        with mock.patch.object(longterm.threading, "Thread", _SyncThread), \
             mock.patch.object(longterm, "call_llm") as llm:
            longterm.digest_async("qq", "9", ["{bad", ""])
        llm.assert_not_called()
        self.assertFalse(os.path.exists(_mem_path(self.root)))


# ─── 读取 + 注入渲染 ────────────────────────────────

def _write_memory(root, group, summaries):
    os.makedirs(os.path.dirname(_mem_path(root, group)), exist_ok=True)
    with open(_mem_path(root, group), "w", encoding="utf-8") as f:
        for i, s in enumerate(summaries):
            f.write(json.dumps({"t": i, "d": "2026-09-%02d" % (i + 1),
                                "n": 200, "s": s}, ensure_ascii=False) + "\n")


class FormatMemoriesTest(_TmpAgentsMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tmp()

    def test_header_and_chronological_order(self):
        _write_memory(self.root, "9", ["第一条", "第二条", "第三条"])
        out = longterm.format_memories("qq", "9", 10, 2000)
        self.assertTrue(out.startswith("[这个群更早的记忆]"))
        self.assertLess(out.index("第一条"), out.index("第三条"))

    def test_missing_file_is_empty(self):
        self.assertEqual(longterm.format_memories("qq", "9", 10, 2000), "")

    def test_limit_takes_most_recent(self):
        _write_memory(self.root, "9", ["一", "二", "三"])
        out = longterm.format_memories("qq", "9", 2, 2000)
        self.assertNotIn("一", out)
        self.assertIn("二", out)
        self.assertIn("三", out)

    def test_max_chars_drops_oldest(self):
        _write_memory(self.root, "9", ["第一" * 50, "第二" * 50, "第三" * 50])
        out = longterm.format_memories("qq", "9", 10, 60)
        self.assertNotIn("第一", out)
        self.assertIn("第三", out)

    def test_zero_disables(self):
        _write_memory(self.root, "9", ["有内容"])
        self.assertEqual(longterm.format_memories("qq", "9", 0, 2000), "")
        self.assertEqual(longterm.format_memories("qq", "9", 10, 0), "")

    def test_broken_line_skipped(self):
        os.makedirs(os.path.dirname(_mem_path(self.root)), exist_ok=True)
        with open(_mem_path(self.root), "w", encoding="utf-8") as f:
            f.write("{bad json\n")
            f.write(json.dumps({"t": 2, "d": "2026-09-02", "s": "好回忆"},
                               ensure_ascii=False) + "\n")
        out = longterm.format_memories("qq", "9", 10, 2000)
        self.assertIn("好回忆", out)
        self.assertNotIn("bad", out)

    def test_groups_are_isolated(self):
        _write_memory(self.root, "9", ["九群的记忆"])
        self.assertIn("九群", longterm.format_memories("qq", "9", 10, 2000))
        self.assertEqual(longterm.format_memories("qq", "8", 10, 2000), "")


# ─── 触发点：裁剪时真的会叫摘要 ─────────────────────

class TrimFiresDigestTest(_TmpAgentsMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tmp()

    def _overflow(self):
        with mock.patch.object(recent, "MAX_LINES", 10), \
             mock.patch.object(recent, "KEEP_LINES", 5), \
             mock.patch.object(longterm, "digest_async") as digest:
            for i in range(12):
                recent.remember("qq", "9", "甲", "m%d" % i)
        return digest

    def test_digest_called_with_dropped_lines(self):
        digest = self._overflow()
        self.assertEqual(digest.call_count, 1)
        args = digest.call_args[0]
        self.assertEqual(args[0], "qq")                     # agent_id
        self.assertEqual(args[1], "9")                      # group_id
        dropped = args[2]
        # MAX_LINES=10：写到第 11 条时第一次超限（11 > 10），丢前 6 条留 5；
        # 之后文件只剩 6 条，不会再触发。
        self.assertEqual(len(dropped), 6)
        texts = [json.loads(l)["x"] for l in dropped]
        self.assertEqual(texts, ["m%d" % i for i in range(6)])

    def test_no_overflow_no_digest(self):
        with mock.patch.object(longterm, "digest_async") as digest:
            recent.remember("qq", "9", "甲", "没超限")
        digest.assert_not_called()

    def test_digest_error_does_not_break_remember(self):
        """摘要入口炸了也不能影响聊天缓存本身。"""
        with mock.patch.object(recent, "MAX_LINES", 10), \
             mock.patch.object(recent, "KEEP_LINES", 5), \
             mock.patch.object(longterm, "digest_async",
                               side_effect=RuntimeError("boom")):
            self.assertTrue(recent.remember("qq", "9", "甲", "超限那条"))
            out = recent.format_recent("qq", "9", 30, 500)
        self.assertIn("超限那条", out)


if __name__ == "__main__":
    unittest.main()
