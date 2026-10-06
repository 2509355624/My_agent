# -*- coding: utf-8 -*-
"""search_agent 测试 —— 只做搜索的那一层。

这一层被用户反复钉死过定位：「唯一任务就是搜索」。所以下面这些**必须有**，
也**必须不能有**：

必须有：拿到用户需求 → 调 search_tags → 返回一份资料。
必须不能有：会话历史、大大怪人设、提示词产出、回话。

还有一条来自真实事故（用户抱怨过「好的，是这个工具吗？」连续刷屏）：
**中间轮（带工具调用的那一轮）的正文一个字都不能往外传**，只有收尾轮的
正文才是资料。

全部用例都 mock 掉 LLM——这里跑的是循环逻辑，不该出网。
"""

import unittest
from unittest import mock

from app import search_agent as sa


class _Base(unittest.TestCase):
    def setUp(self):
        # 工具也 mock 掉：跑的是循环，不是标签库 / 网络。
        # 必须 patch `_TOOLS` 这张表——`_run_tool` 是从它取函数的，
        # 只 patch `sa.search_tags` 已经不起作用（2026-10-06 加了第二个工具
        # web_search，把这张表做成了唯一入口）。
        self.tool = mock.Mock(
            side_effect=lambda q: "【命中】%s → tag_%s" % (q, q))
        self.web = mock.Mock(side_effect=lambda q, n=5: "网页结果：%s" % q)
        p = mock.patch.dict(sa._TOOLS, {"search_tags": self.tool,
                                        "web_search": self.web})
        p.start()
        self.addCleanup(p.stop)

    def _llm(self, replies):
        """按顺序返回 replies；超出后重复最后一个。记录收到的 messages。"""
        seen = []

        def fake(messages, timeout=None):
            seen.append([dict(m) for m in messages])
            i = min(len(seen) - 1, len(replies) - 1)
            return replies[i]

        p = mock.patch.object(sa.llm, "call_llm", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return seen


class BasicTest(_Base):
    def test_empty_need_returns_empty_without_calling_llm(self):
        seen = self._llm(["不该被调用"])
        self.assertEqual(sa.search(""), "")
        self.assertEqual(sa.search("   "), "")
        self.assertEqual(len(seen), 0)

    def test_direct_answer_needs_only_one_round(self):
        """模型不需要查库时，一轮就返回资料。"""
        seen = self._llm(["角色：胡桃 → hu_tao_(genshin_impact)（原神）"])
        out = sa.search("画个胡桃")
        self.assertEqual(out, "角色：胡桃 → hu_tao_(genshin_impact)（原神）")
        self.assertEqual(len(seen), 1)


class LoopTest(_Base):
    def test_tool_call_then_doc(self):
        seen = self._llm([
            '[[TOOL:search_tags]]{"query": "胡桃"}',
            "角色：胡桃 → hu_tao_(genshin_impact)（原神）",
        ])
        out = sa.search("画个胡桃")
        self.assertIn("hu_tao_(genshin_impact)", out)
        self.assertEqual(len(seen), 2)
        self.tool.assert_called_once_with("胡桃")

    def test_tool_result_is_fed_back_to_the_model(self):
        seen = self._llm([
            '[[TOOL:search_tags]]{"query": "银狼"}',
            "资料",
        ])
        sa.search("画个银狼")
        second = seen[1]
        joined = "\n".join(m["content"] for m in second)
        self.assertIn("tag_银狼", joined)

    def test_intermediate_text_is_never_returned(self):
        """核心回归：中间轮正文不许外泄。

        真实事故——模型每轮都吐一句「好的，是这个工具吗？」，全被发出去，
        用户看到连续几轮刷屏。只有**不带工具调用**的那一轮正文才是资料。
        """
        self._llm([
            '好的，是这个工具吗？\n[[TOOL:search_tags]]{"query": "胡桃"}',
            '好的好的，是这个工具吗？\n[[TOOL:search_tags]]{"query": "原神"}',
            "角色：胡桃 → hu_tao_(genshin_impact)",
        ])
        out = sa.search("画个胡桃")
        self.assertEqual(out, "角色：胡桃 → hu_tao_(genshin_impact)")
        self.assertNotIn("是这个工具吗", out)

    def test_multiple_tool_calls_in_one_round(self):
        seen = self._llm([
            '[[TOOL:search_tags]]{"query": "胡桃"}\n'
            '[[TOOL:search_tags]]{"query": "原神"}',
            "资料",
        ])
        sa.search("画个胡桃")
        self.assertEqual(self.tool.call_count, 2)
        joined = "\n".join(m["content"] for m in seen[1])
        self.assertIn("tag_胡桃", joined)
        self.assertIn("tag_原神", joined)

    def test_round_cap_is_enforced(self):
        """模型一直查个不停时，必须在 max_rounds 处停下。"""
        seen = self._llm(['[[TOOL:search_tags]]{"query": "x"}'])
        out = sa.search("画个胡桃", max_rounds=3)
        self.assertEqual(len(seen), 3)
        self.assertEqual(self.tool.call_count, 3)
        # 从没进过收尾轮 → 没有整理好的资料，但**绝不能返回空**：
        # 把最后一轮的工具结果兜出去。2026-10-06 实录：5 轮 40+ 次调用、
        # 6,199 miss token，最后返回空串，整场全白烧。
        self.assertIn("tag_x", out)

    def test_default_round_cap_is_ten(self):
        self.assertEqual(sa.MAX_ROUNDS, 10)

    def test_unknown_tool_is_rejected_not_executed(self):
        self._llm([
            '[[TOOL:generate_image]]{"prompt": "x"}',
            "资料",
        ])
        sa.search("画个胡桃")
        self.tool.assert_not_called()

    def test_missing_query_does_not_call_the_tool(self):
        self._llm(['[[TOOL:search_tags]]{}', "资料"])
        sa.search("画个胡桃")
        self.tool.assert_not_called()


class FailureTest(_Base):
    def test_llm_exception_returns_empty(self):
        p = mock.patch.object(sa.llm, "call_llm", side_effect=RuntimeError("boom"))
        p.start()
        self.addCleanup(p.stop)
        self.assertEqual(sa.search("画个胡桃"), "")

    def test_empty_reply_returns_empty(self):
        self._llm(["", "  "])
        self.assertEqual(sa.search("画个胡桃"), "")

    def test_tool_exception_does_not_propagate(self):
        p = mock.patch.dict(
            sa._TOOLS,
            {"search_tags": mock.Mock(side_effect=RuntimeError("库炸了"))})
        p.start()
        self.addCleanup(p.stop)
        self._llm(['[[TOOL:search_tags]]{"query": "胡桃"}', "资料"])
        self.assertEqual(sa.search("画个胡桃"), "资料")


class DocShapeTest(_Base):
    def test_doc_is_truncated_to_the_hard_cap(self):
        self._llm(["啊" * 5000])
        out = sa.search("画个胡桃")
        self.assertLessEqual(len(out), sa.MAX_DOC_CHARS + 1)
        self.assertTrue(out.endswith("…"))

    def test_strip_removes_both_tool_block_families(self):
        """`_strip` 是纯函数，两族工具块都要剥干净。

        自己写正则会漏掉 XML 族（`<function=…>`），或者在方括号族上把
        工具块后面的正文一起吃掉——两种都见过，所以这里两族都钉。
        """
        cases = (
            '正文\n[[TOOL:search_tags]]{"query": "x"}\n结尾',
            '正文\n<function=search_tags><parameter=query>x</parameter></function>\n结尾',
        )
        for raw in cases:
            out = sa._strip(raw)
            self.assertIn("正文", out)
            self.assertIn("结尾", out)
            self.assertNotIn("search_tags", out)


class ScopeTest(_Base):
    """定位守卫：这一层只该有搜索，多一样东西都是错的。"""

    def test_prompt_has_no_history_placeholder(self):
        for bad in ("最近对话", "历史", "{recent}", "{text}", "{chan_hint}"):
            self.assertNotIn(bad, sa.SEARCH_PROMPT)

    def test_prompt_offers_the_two_tools_only(self):
        self.assertIn("search_tags", sa.SEARCH_PROMPT)
        self.assertIn("web_search", sa.SEARCH_PROMPT)
        self.assertNotIn("generate_image", sa.SEARCH_PROMPT)

    def test_web_search_is_gated_to_last_resort(self):
        """网页搜索贵得多，提示词必须把它限成「查不到 / 拿不准才用」。"""
        self.assertIn("只在标签库查不到", sa.SEARCH_PROMPT)

    def test_prompt_forces_a_tool_call_on_round_one(self):
        """真实事故：模型第 1 轮直接凭记忆写资料，冷门角色的 tag 全是编的。"""
        self.assertIn("第一轮必须先调工具", sa.SEARCH_PROMPT)

    def test_tool_table_has_exactly_two_entries(self):
        self.assertEqual(set(sa._TOOLS), {"search_tags", "web_search"})

    def test_prompt_forbids_inventing_tags(self):
        self.assertIn("不许编", sa.SEARCH_PROMPT)

    def test_prompt_caps_doc_length(self):
        self.assertIn("1000 字", sa.SEARCH_PROMPT)

    def test_need_is_passed_verbatim(self):
        seen = self._llm(["资料"])
        sa.search("画个银狼在打游戏")
        self.assertIn("画个银狼在打游戏", seen[0][0]["content"])

    def test_single_message_no_system_role(self):
        """跟项目里其它 LLM 调用一致：全部塞进一条 user 消息。"""
        seen = self._llm(["资料"])
        sa.search("画个胡桃")
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(seen[0][0]["role"], "user")


class WebSearchTest(_Base):
    """第二个工具：联网搜索（用户 2026-10-06 要求加上）。"""

    def test_web_search_call_is_routed(self):
        seen = self._llm([
            '[[TOOL:web_search]]{"query": "银狼 崩坏星穹铁道"}',
            "资料",
        ])
        sa.search("画个银狼")
        self.web.assert_called_once()
        self.assertEqual(self.web.call_args[0][0], "银狼 崩坏星穹铁道")
        joined = "\n".join(m["content"] for m in seen[1])
        self.assertIn("网页结果", joined)

    def test_search_tags_not_touched_when_web_is_used(self):
        self._llm(['[[TOOL:web_search]]{"query": "x"}', "资料"])
        sa.search("画个胡桃")
        self.tool.assert_not_called()

    def test_max_results_is_clamped_to_ten(self):
        self._llm(['[[TOOL:web_search]]{"query": "x", "max_results": 999}', "资料"])
        sa.search("画个胡桃")
        self.assertEqual(self.web.call_args[0][1], 10)

    def test_bad_max_results_falls_back_to_five(self):
        self._llm(['[[TOOL:web_search]]{"query": "x", "max_results": "多"}', "资料"])
        sa.search("画个胡桃")
        self.assertEqual(self.web.call_args[0][1], 5)

    def test_tool_result_is_truncated(self):
        """工具结果要一直留在 messages 里，不截断 10 轮就能把上下文撑爆。"""
        self.web.side_effect = lambda q, n=5: "长" * 9000
        seen = self._llm(['[[TOOL:web_search]]{"query": "x"}', "资料"])
        sa.search("画个胡桃")
        tool_msg = seen[1][-1]["content"]        # 最后一轮追加的那条工具结果
        self.assertIn("结果已截断", tool_msg)
        self.assertLess(len(tool_msg), sa.MAX_TOOL_CHARS + 200)

    def test_both_tools_in_one_round(self):
        seen = self._llm([
            '[[TOOL:search_tags]]{"query": "胡桃"}\n'
            '[[TOOL:web_search]]{"query": "胡桃 原神"}',
            "资料",
        ])
        sa.search("画个胡桃")
        self.tool.assert_called_once_with("胡桃")
        self.web.assert_called_once()
        joined = "\n".join(m["content"] for m in seen[1])
        self.assertIn("tag_胡桃", joined)
        self.assertIn("网页结果", joined)


if __name__ == "__main__":
    unittest.main()
