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
    """用生产口径的阈值做断言（预算 32000、预警线 75%、冷却 20%），
    这样测试同时起到「阈值文档」的作用。水位按 token 绝对量比较，
    不再是「占某个上限的百分比」。
    """

    BUDGET = 32000

    def setUp(self):
        # _LAST_COMPACT_TOKENS 是按 agent 记的字典，压缩一次就被改写；每例清空
        for target, value in (
            ("_LAST_COMPACT_TOKENS", {}),
            ("_summarize_old_turns", lambda msgs, **kw: "旧内容摘要"),
        ):
            p = mock.patch.object(memory, target, value)
            p.start()
            self.addCleanup(p.stop)

    def _trim(self, history, total_tokens, hit_rate, budget=None):
        """显式传用量跑一次判断。

        不依赖 llm 里那份「按线程记」的记录——显式传参才是 agent 循环真正
        走的路径，测试之间也不会互相影响。
        """
        return memory.trim_history(history, usage={
            "total_tokens": total_tokens, "hit_tokens": 0,
            "miss_tokens": 0, "hit_rate": hit_rate,
        }, budget=budget or self.BUDGET)

    @staticmethod
    def _history_with_turns(n):
        h = [_msg("system", "sys")]
        for i in range(n):
            h.append(_msg("user", "u%d" % i))
            h.append(_msg("assistant", "a%d" % i))
        return h

    def test_below_warn_line_not_compacted(self):
        h = self._history_with_turns(6)
        self.assertIs(self._trim(h, 10_000, 0.0), h)      # 远低于预警线

    def test_high_hit_rate_above_warn_line_not_compacted(self):
        h = self._history_with_turns(6)
        # 过了预警线（24000）但命中率高 → 让热前缀自然增长更划算
        self.assertIs(self._trim(h, 26_000, 0.9), h)

    def test_low_hit_rate_above_warn_line_triggers_compaction(self):
        h = self._history_with_turns(6)
        out = self._trim(h, 26_000, 0.1)                  # 且命中率低 → 压缩
        self.assertIsNot(out, h)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[1]["tool_name"], "compact_summary")
        self.assertIn("旧内容摘要", out[1]["content"])
        # 最近 FULL_RECENT_TURNS 轮必须完整保留（热前缀锚点）
        self.assertEqual(out[-1]["content"], "a5")

    def test_budget_reached_forces_compaction_despite_high_hit(self):
        h = self._history_with_turns(6)
        # 到预算就是硬闸门：命中率再高也得压。这是「最坏花多少钱」的唯一保证，
        # 少了它上下文会一路涨，每轮都在为这一长串付钱。
        out = self._trim(h, 33_000, 1.0)
        self.assertIsNot(out, h)
        self.assertEqual(out[1]["tool_name"], "compact_summary")

    def test_custom_budget_shifts_the_line(self):
        h = self._history_with_turns(6)
        # 同样是 26K：32K 预算下只到预警区（命中率高就不压），
        # 换成 16K 预算已经越线，必须压。证明 agent 级预算真的生效。
        self.assertIs(self._trim(h, 26_000, 0.9), h)
        self.assertIsNot(self._trim(h, 26_000, 0.9, budget=16_000), h)

    def test_cooldown_blocks_repeat_compaction(self):
        h = self._history_with_turns(6)
        self._trim(h, 26_000, 0.1)                        # 第一次压缩，记录水位
        self.assertIs(self._trim(h, 26_000, 0.1), h)      # 增量 0 < 冷却阈值 → 跳过

    def test_cooldown_does_not_block_forced_compaction(self):
        h = self._history_with_turns(6)
        self._trim(h, 26_000, 0.1)                        # 先把水位推到 26000
        # 同一水位上涨到预算之上：强制压缩不受冷却限制
        self.assertIsNot(self._trim(h, 33_000, 0.1), h)

    def test_too_few_turns_returns_unchanged(self):
        h = self._history_with_turns(2)                   # 轮数 <= FULL_RECENT_TURNS
        self.assertIs(self._trim(h, 33_000, 0.0), h)

    def test_only_system_messages_returned_as_is(self):
        h = [_msg("system", "sys")]
        self.assertIs(self._trim(h, 33_000, 0.0), h)


class CompactKeepDegradeTest(unittest.TestCase):
    """摘要之后仍然超预算时，保留轮数要自动递减。

    这是「最近几轮自己就很大」的兜底：不加它，摘要省下的空间会被原样留下
    的那几轮吃回去，于是每轮都超支、每轮都要摘要一次。
    """

    def setUp(self):
        p = mock.patch.object(memory, "_summarize_old_turns",
                              lambda msgs, **kw: "旧内容摘要")
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _fat_history(turns, chars):
        """造一份每轮都很胖的历史（汉字 ≈ 0.6 token/字）。"""
        h = [_msg("system", "sys")]
        for i in range(turns):
            h.append(_msg("user", "u%d" % i + "字" * chars))
            h.append(_msg("assistant", "a%d" % i + "字" * chars))
        return h

    @staticmethod
    def _kept_turns(out):
        return len([m for m in out if m.get("role") == "user"])

    def test_keep_degrades_when_recent_turns_too_big(self):
        # 每轮约 2×4000 字 ≈ 4800 token。预算 12000 装不下最近 3 轮，
        # 于是只能再少留一轮
        h = self._fat_history(6, 4000)
        out = memory.trim_history(h, usage={"total_tokens": 40_000,
                                            "hit_rate": 0.0}, budget=12_000)
        self.assertIsNot(out, h)
        self.assertLess(self._kept_turns(out), memory.FULL_RECENT_TURNS)
        # 最新的那轮无论如何都要留着
        self.assertIn("a5", out[-1]["content"])

    def test_keep_stays_when_it_fits(self):
        h = self._fat_history(6, 10)                     # 每轮很小
        out = memory.trim_history(h, usage={"total_tokens": 40_000,
                                            "hit_rate": 0.0}, budget=12_000)
        self.assertEqual(self._kept_turns(out), memory.FULL_RECENT_TURNS)

    def test_summary_covers_the_dropped_turns(self):
        seen = []
        with mock.patch.object(memory, "_summarize_old_turns",
                               lambda msgs, **kw: seen.append(msgs) or "摘要"):
            h = self._fat_history(6, 4000)
            memory.trim_history(h, usage={"total_tokens": 40_000,
                                          "hit_rate": 0.0}, budget=12_000)
        # 被降级砍掉的那几轮要进摘要输入，而不是直接丢掉
        self.assertTrue(seen)
        joined = "\n".join(str(m.get("content")) for m in seen[0])
        self.assertIn("u0", joined)


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


class SummaryModelPassthroughTest(unittest.TestCase):
    """摘要调用必须带上**本轮生效**的 provider/model。

    摘要是后台的隐形调用：漏传时 get_effective_config 会回退到 .env 的
    全局默认，于是同一轮里主对话用 agent 配的模型、压缩却打另一家的额度。
    实测症状：qq agent 已切到 deepseek，一触发压缩就报火山的额度错误，
    看起来像"有些群没切过来"，其实是压缩这条支线从来不读 agent 配置。
    """

    BUDGET = 12_000

    def setUp(self):
        # 压缩水位按 agent 记，跨用例互相污染；清空更干净
        p = mock.patch.object(memory, "_LAST_COMPACT_TOKENS", {})
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _fat_history(turns=6, chars=4000):
        """每轮约 4800 token，预算 12000 装不下最近 3 轮 → 必然触发摘要。"""
        h = [_msg("system", "sys")]
        for i in range(turns):
            h.append(_msg("user", "u%d" % i + "字" * chars))
            h.append(_msg("assistant", "a%d" % i + "字" * chars))
        return h

    def _trim(self, **kw):
        return memory.trim_history(
            self._fat_history(),
            usage={"total_tokens": 40_000, "hit_rate": 0.0},
            budget=self.BUDGET, **kw)

    def test_trim_history_forwards_provider_and_model(self):
        seen = {}

        def fake(msgs, provider=None, model=None):
            seen["provider"], seen["model"] = provider, model
            return "摘要"

        with mock.patch.object(memory, "_summarize_old_turns", fake):
            self._trim(provider="deepseek", model="deepseek-flash")

        self.assertEqual(seen, {"provider": "deepseek",
                                "model": "deepseek-flash"})

    def test_call_llm_is_invoked_with_them(self):
        """端到端落到 call_llm 的关键字参数——这一层漏了，上一层就白传。"""
        seen = {}

        def fake_call_llm(messages, timeout=600, provider=None, model=None):
            seen["provider"], seen["model"] = provider, model
            return "摘要"

        with mock.patch("app.llm.call_llm", fake_call_llm):
            memory._summarize_old_turns([_msg("user", "旧")],
                                        provider="deepseek",
                                        model="deepseek-flash")

        self.assertEqual(seen, {"provider": "deepseek",
                                "model": "deepseek-flash"})

    def test_omitting_them_keeps_old_behaviour(self):
        """老调用点不传时行为不变：None 透传，由 llm 层回退全局默认。"""
        seen = {}

        def fake(msgs, provider=None, model=None):
            seen["provider"], seen["model"] = provider, model
            return "摘要"

        with mock.patch.object(memory, "_summarize_old_turns", fake):
            self._trim()

        self.assertEqual(seen, {"provider": None, "model": None})


class SummaryFailureDegradeTest(unittest.TestCase):
    """摘要失败降级为「本轮不压缩」，绝不把整轮对话一起带走。"""

    BUDGET = 12_000

    def setUp(self):
        p = mock.patch.object(memory, "_LAST_COMPACT_TOKENS", {})
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _fat_history():
        h = [_msg("system", "sys")]
        for i in range(6):
            h.append(_msg("user", "u%d" % i + "字" * 4000))
            h.append(_msg("assistant", "a%d" % i + "字" * 4000))
        return h

    def test_failure_returns_history_untouched(self):
        h = self._fat_history()

        def boom(msgs, **kw):
            raise RuntimeError("LLM 请求失败 HTTP 429 | 额度已用尽")

        with mock.patch.object(memory, "_summarize_old_turns", boom):
            out = memory.trim_history(h,
                                      usage={"total_tokens": 40_000,
                                             "hit_rate": 0.0},
                                      budget=self.BUDGET, provider="deepseek")

        self.assertIs(out, h)          # 原样交回，调用方照常往下走

    def test_failure_does_not_propagate(self):
        def boom(msgs, **kw):
            raise ConnectionError("网络断了")

        with mock.patch.object(memory, "_summarize_old_turns", boom):
            try:
                memory.trim_history(self._fat_history(),
                                    usage={"total_tokens": 40_000,
                                           "hit_rate": 0.0},
                                    budget=self.BUDGET, provider="deepseek")
            except Exception as e:
                self.fail("摘要失败不该抛出，实际抛了 %r" % (e,))


if __name__ == "__main__":
    unittest.main()
