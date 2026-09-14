# -*- coding: utf-8 -*-
"""工具调用协议解析测试（app/agent.py）。

这是整个 Agent 的"协议层"，也是最少见、最容易被改坏的一层——为了让
本地小模型（会漏 `TOOL:` 前缀、大小写乱写、冒号后加空格）也能用，
这里刻意做了宽容解析。下面把这些宽容规则逐条钉死。

覆盖：
- parse_tool_calls：标准/简写/大小写/空白/缺关闭标签/多调用/非 JSON 参数
- _strip_tool_blocks：剥离工具块但保留正文
- _history_for_llm：内部 tool_result -> LLM 的 user role 转换
"""

import unittest

from app.agent import (parse_tool_calls, _strip_tool_blocks, _history_for_llm,
                       _iter_tool_tags)


class ParseToolCallsTest(unittest.TestCase):
    def test_standard_form(self):
        self.assertEqual(parse_tool_calls('[[TOOL:list_skills]][[/TOOL]]'),
                         [{"name": "list_skills", "args": {}}])

    def test_json_args(self):
        text = '[[TOOL:read_file]]{"skill_name": "sun", "filename": "skill.md"}[[/TOOL]]'
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "read_file")
        self.assertEqual(calls[0]["args"],
                         {"skill_name": "sun", "filename": "skill.md"})

    def test_nested_braces_in_args(self):
        text = '[[TOOL:ingest_kb]]{"entries": [{"id": "a"}]}[[/TOOL]]'
        self.assertEqual(parse_tool_calls(text)[0]["args"],
                         {"entries": [{"id": "a"}]})

    def test_whitespace_after_colon(self):
        self.assertEqual(parse_tool_calls('[[TOOL: list_skills]]')[0]["name"],
                         "list_skills")
        self.assertEqual(parse_tool_calls('[[TOOL :list_skills]]')[0]["name"],
                         "list_skills")

    def test_uppercase_name_normalized_to_registry(self):
        self.assertEqual(parse_tool_calls('[[TOOL:LIST_SKILLS]]')[0]["name"],
                         "list_skills")

    def test_missing_closing_tag(self):
        self.assertEqual(parse_tool_calls('[[TOOL:list_skills]]')[0]["name"],
                         "list_skills")

    def test_short_form_recognized_when_name_known(self):
        # 小模型常见写法：省略 TOOL: 前缀，但名字能命中注册表 → 认
        self.assertEqual(parse_tool_calls('[[list_skills]]')[0]["name"],
                         "list_skills")

    def test_short_form_unknown_is_ignored(self):
        # 未命中注册表又没前缀 → 当作正文标记，不能误判成工具
        self.assertEqual(parse_tool_calls('这是一段 [[note]] 正文'), [])

    def test_unknown_name_with_prefix_is_kept(self):
        # 带前缀但工具不存在 → 保留，交给执行层报"未知工具"
        calls = parse_tool_calls('[[TOOL:ghost_tool]]{}')
        self.assertEqual([c["name"] for c in calls], ["ghost_tool"])

    def test_multiple_calls_in_one_reply(self):
        text = '先看看\n[[TOOL:list_skills]][[/TOOL]]\n[[TOOL:get_time]][[/TOOL]]'
        self.assertEqual([c["name"] for c in parse_tool_calls(text)],
                         ["list_skills", "get_time"])

    def test_non_json_args_fall_back_to_raw(self):
        calls = parse_tool_calls('[[TOOL:read_file]]{not json}')
        self.assertEqual(calls[0]["args"], {"raw": "{not json}"})

    def test_closing_tag_is_not_a_tool_call(self):
        self.assertEqual(parse_tool_calls('[[/TOOL]]'), [])

    def test_empty_inputs(self):
        self.assertEqual(parse_tool_calls(''), [])
        self.assertEqual(parse_tool_calls(None), [])


class IterToolTagsTest(unittest.TestCase):
    def test_yields_match_and_normalized_name(self):
        tags = list(_iter_tool_tags('[[TOOL:LIST_SKILLS]]'))
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0][1], "list_skills")

    def test_prefix_is_case_insensitive(self):
        tags = list(_iter_tool_tags('[[tool:list_skills]]'))
        self.assertEqual(tags[0][1], "list_skills")


class StripToolBlocksTest(unittest.TestCase):
    def test_removes_block_but_keeps_prose(self):
        text = '我来查一下。\n[[TOOL:get_time]][[/TOOL]]\n稍等。'
        out = _strip_tool_blocks(text)
        self.assertNotIn("TOOL:", out)
        self.assertIn("我来查一下。", out)
        self.assertIn("稍等。", out)

    def test_removes_json_args_too(self):
        text = 'ok [[TOOL:read_file]]{"skill_name": "a", "filename": "b.md"}[[/TOOL]] done'
        out = _strip_tool_blocks(text)
        self.assertNotIn("read_file", out)
        self.assertNotIn("filename", out)
        self.assertIn("ok", out)
        self.assertIn("done", out)

    def test_removes_short_form(self):
        out = _strip_tool_blocks('看 [[list_skills]]')
        self.assertNotIn("list_skills", out)
        self.assertIn("看", out)

    def test_unknown_short_form_left_untouched(self):
        text = '引用 [[note]] 标记'
        self.assertEqual(_strip_tool_blocks(text), text)

    def test_nested_json_args_swallowed_whole(self):
        text = '[[TOOL:ingest_kb]]{"entries": [{"id": "x", "content": "y"}]}[[/TOOL]]'
        self.assertEqual(_strip_tool_blocks(text).strip(), '')

    def test_empty_input(self):
        self.assertEqual(_strip_tool_blocks(''), '')


class HistoryForLlmTest(unittest.TestCase):
    def test_tool_result_becomes_user_with_prefix(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "tool_result", "content": "res", "tool_name": "get_time"},
            {"role": "assistant", "content": "ok"},
        ]
        out = _history_for_llm(history)
        self.assertEqual(out[1]["role"], "user")
        self.assertEqual(out[1]["content"], "[工具结果] res")
        # 其它消息原样透传
        self.assertEqual(out[0], history[0])
        self.assertEqual(out[2], history[2])


if __name__ == "__main__":
    unittest.main()
