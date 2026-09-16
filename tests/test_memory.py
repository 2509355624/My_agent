# -*- coding: utf-8 -*-
"""会话持久化（原子写）与缓存感知上下文压缩测试（app/memory.py）。

两类关注点：
1. 持久化：原子写不能留下半截文件 / 临时文件，且能容忍损坏行；
2. 压缩：只在"占用率高 + 命中率低"或"接近上限"时触发，且有冷却，
   目的是保住 prefix cache 的热前缀——阈值写错会直接让缓存收益归零。
"""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import app.agents as agents
import app.memory as memory


def _msg(role, content="x"):
    return {"role": role, "content": content}


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 会话文件现在由 agent 决定：agents/<id>/session.jsonl。
        # 把 agents 目录整体重定向到临时目录，就不会碰真实的 agents/。
        p = mock.patch.object(agents, "AGENTS_DIR",
                              os.path.join(self.tmp.name, "agents"))
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        self.path = os.path.join(self.tmp.name, "agents", "main", "session.jsonl")

    def test_roundtrip_preserves_unicode(self):
        history = [_msg("system", "系统"),
                   _msg("user", "你好，世界 🌏")]
        memory.save_history(history)
        self.assertEqual(memory.load_history(), history)

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(memory.load_history(), [])

    def test_overwrite_keeps_only_latest(self):
        memory.save_history([_msg("user", "第一次")])
        memory.save_history([_msg("user", "第二次")])
        loaded = memory.load_history()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["content"], "第二次")

    def test_no_leftover_tmp_file(self):
        memory.save_history([_msg("user", "x")])
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["session.jsonl"])

    def test_corrupted_line_is_skipped(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "content": "ok"},
                               ensure_ascii=False) + "\n")
            f.write("{这不是合法 JSON\n")
            f.write("\n")  # 空行
            f.write(json.dumps({"role": "assistant", "content": "hi"}) + "\n")
        self.assertEqual([m["content"] for m in memory.load_history()],
                         ["ok", "hi"])

    def test_two_agents_keep_separate_sessions(self):
        """会话隔离：这是多 agent 的核心语义——各写各的文件，互不覆盖。"""
        memory.save_history([_msg("user", "main 的会话")], "main")
        memory.save_history([_msg("user", "writing 的会话")], "writing")
        self.assertEqual([m["content"] for m in memory.load_history("main")],
                         ["main 的会话"])
        self.assertEqual([m["content"] for m in memory.load_history("writing")],
                         ["writing 的会话"])
        self.assertNotEqual(agents.session_file("main"),
                            agents.session_file("writing"))
        self.assertTrue(os.path.exists(agents.session_file("main")))
        self.assertTrue(os.path.exists(agents.session_file("writing")))

    def test_illegal_agent_id_never_writes_outside(self):
        """agent_id 带 ../ 时兜底到默认 agent，绝不在 agents/ 之外落文件。"""
        memory.save_history([_msg("user", "兜底")], "../evil")
        self.assertTrue(os.path.exists(agents.session_file("main")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil")))

    def test_concurrent_saves_never_produce_partial_file(self):
        payloads = [[_msg("user", "msg-%d" % i)] * 50 for i in range(8)]
        errors = []

        def worker(hist):
            try:
                for _ in range(20):
                    memory.save_history(hist)
            except Exception as e:  # pragma: no cover - 出错时用于报告
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # 最终文件必须是完整可解析的 50 行，不能是两次写入交错的结果
        self.assertEqual(len(memory.load_history()), 50)


class TrimHistoryTest(unittest.TestCase):
    """用生产口径的阈值做断言（CONTEXT_LIMIT=1M、冷却 10 万 token），
    这样测试同时起到"阈值文档"的作用。占用率按 1M 上下文换算。
    """

    def setUp(self):
        # _LAST_COMPACT_TOKENS 是按 agent 记的字典，压缩一次就被改写；每例清空
        for target, value in (
            ("_LAST_COMPACT_TOKENS", {}),
            ("_summarize_old_turns", lambda msgs: "旧内容摘要"),
        ):
            p = mock.patch.object(memory, target, value)
            p.start()
            self.addCleanup(p.stop)

    def _set_usage(self, total_tokens, hit_rate):
        p = mock.patch.object(memory, "LAST_USAGE", {
            "total_tokens": total_tokens, "hit_tokens": 0,
            "miss_tokens": 0, "hit_rate": hit_rate,
        })
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _history_with_turns(n):
        h = [_msg("system", "sys")]
        for i in range(n):
            h.append(_msg("user", "u%d" % i))
            h.append(_msg("assistant", "a%d" % i))
        return h

    def test_below_threshold_not_compacted(self):
        self._set_usage(100_000, 0.0)  # 占用 10%
        h = self._history_with_turns(6)
        self.assertIs(memory.trim_history(h), h)

    def test_high_hit_rate_at_compact_ratio_not_compacted(self):
        self._set_usage(850_000, 0.9)  # 占用 85% 但命中率高 → 让热前缀自然增长
        h = self._history_with_turns(6)
        self.assertIs(memory.trim_history(h), h)

    def test_low_hit_rate_triggers_compaction(self):
        self._set_usage(850_000, 0.1)  # 占用 85% 且命中率低 → 压缩
        h = self._history_with_turns(6)
        out = memory.trim_history(h)
        self.assertIsNot(out, h)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[1]["tool_name"], "compact_summary")
        self.assertIn("旧内容摘要", out[1]["content"])
        # 最近 FULL_RECENT_TURNS 轮必须完整保留（热前缀锚点）
        self.assertEqual(out[-1]["content"], "a5")

    def test_reactive_ratio_forces_compaction_despite_high_hit(self):
        self._set_usage(950_000, 1.0)  # 占用 95% → 无论命中率都压缩
        h = self._history_with_turns(6)
        out = memory.trim_history(h)
        self.assertIsNot(out, h)
        self.assertEqual(out[1]["tool_name"], "compact_summary")

    def test_cooldown_blocks_repeat_compaction(self):
        self._set_usage(850_000, 0.1)
        h = self._history_with_turns(6)
        memory.trim_history(h)                       # 第一次压缩，记录水位
        self.assertIs(memory.trim_history(h), h)     # 增量 0 < 冷却阈值 → 跳过

    def test_too_few_turns_returns_unchanged(self):
        self._set_usage(950_000, 0.0)
        h = self._history_with_turns(2)              # 轮数 <= FULL_RECENT_TURNS
        self.assertIs(memory.trim_history(h), h)

    def test_only_system_messages_returned_as_is(self):
        self._set_usage(950_000, 0.0)
        h = [_msg("system", "sys")]
        self.assertIs(memory.trim_history(h), h)


class SplitAndDumpTest(unittest.TestCase):
    def test_split_turns_groups_from_user_message(self):
        msgs = [_msg("user", "u1"), _msg("assistant", "a1"),
                _msg("user", "u2"), _msg("tool_result", "t2"),
                _msg("assistant", "a2")]
        turns = memory._split_turns(msgs)
        self.assertEqual(len(turns), 2)
        self.assertEqual([m["content"] for m in turns[0]], ["u1", "a1"])
        self.assertEqual([m["content"] for m in turns[1]], ["u2", "t2", "a2"])

    def test_dump_messages_marks_roles(self):
        msgs = [_msg("user", "你好"), _msg("assistant", "在"),
                {"role": "tool_result", "tool_name": "get_time", "content": "12:00"}]
        text = memory._dump_messages(msgs)
        self.assertIn("[用户] 你好", text)
        self.assertIn("[助手] 在", text)
        self.assertIn("[工具结果：get_time] 12:00", text)

    def test_dump_truncates_long_tool_result(self):
        msgs = [{"role": "tool_result", "tool_name": "read_document",
                 "content": "x" * 5000}]
        self.assertLess(len(memory._dump_messages(msgs)), 5000)


if __name__ == "__main__":
    unittest.main()
