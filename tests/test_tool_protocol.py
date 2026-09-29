# -*- coding: utf-8 -*-
"""工具调用协议解析测试（app/agent.py）。

这是整个 Agent 的"协议层"，也是最少见、最容易被改坏的一层——为了让
本地小模型（会漏 `TOOL:` 前缀、大小写乱写、冒号后加空格）也能用，
这里刻意做了宽容解析。下面把这些宽容规则逐条钉死。

覆盖：
- parse_tool_calls：标准/简写/大小写/空白/缺关闭标签/多调用/非 JSON 参数
- 畸形形态容错：尖括号开头（<TOOL:x]]）、<tool_call> 外壳——2026-09-29 从
  233的粉丝群实测抓到的，8 次工具调用因此漏成正文发到群里
- Anthropic 式 XML（<function=name><parameter=k>v</parameter></function>）——
  2026-09-29 从清酒瓶子的私聊实测抓到的，84 次工具调用因此没执行、整段
  XML 连着提示词发进了聊天
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


class MalformedFormTest(unittest.TestCase):
    """模型偶尔吐出的畸形形态（2026-09-29 真机取证）。

    样本来自 233的粉丝群会话文件：368 次工具调用里 8 次写成
    `<tool_call><TOOL:name]]{json}[[/TOOL]]`——闭合那半是对的，只有开头把
    `[[` 写成了 `<`，还多套了一层 `<tool_call>`。整块因为不匹配被当正文
    发进群里，群友直接看到工具源码。
    """

    def test_angle_bracket_opening_accepted(self):
        self.assertEqual(parse_tool_calls('<TOOL:list_skills]]'),
                         [{"name": "list_skills", "args": {}}])

    def test_real_leaked_sample_parses(self):
        # 逐字取自群里那条泄漏消息
        text = '<tool_call><TOOL:send_sticker]]{"nums": "11"}[[/TOOL]]'
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "send_sticker")
        self.assertEqual(calls[0]["args"], {"nums": "11"})

    def test_angle_opening_with_json_args(self):
        calls = parse_tool_calls('<TOOL:generate_image]]{"prompt": "x"}[[/TOOL]]')
        self.assertEqual(calls[0]["name"], "generate_image")
        self.assertEqual(calls[0]["args"], {"prompt": "x"})

    def test_bare_angle_form_needs_registry_hit(self):
        # 无 TOOL: 前缀时仍要求名字命中注册表，正文里的 <note]] 不能误判
        self.assertEqual(parse_tool_calls('正文 <note]] 标记'), [])

    def test_angle_closing_alone_is_not_a_call(self):
        self.assertEqual(parse_tool_calls('</TOOL]]'), [])
        self.assertEqual(parse_tool_calls('<TOOL]]'), [])

    def test_strip_swallows_wrapper_and_block(self):
        text = '<tool_call><TOOL:send_sticker]]{"nums": "11"}[[/TOOL]]'
        self.assertEqual(_strip_tool_blocks(text).strip(), '')

    def test_strip_swallows_wrapper_before_standard_tag(self):
        # 另一种变体：标签本身是对的，只是外面多套了壳
        out = _strip_tool_blocks('我来查一下 <tool_call>[[TOOL:list_skills]][[/TOOL]]')
        self.assertIn("我来查一下", out)
        self.assertNotIn("tool_call", out)
        self.assertNotIn("TOOL:", out)

    def test_strip_swallows_closing_wrapper(self):
        out = _strip_tool_blocks('[[TOOL:get_time]][[/TOOL]]</tool_call> 好了')
        self.assertNotIn("tool_call", out)
        self.assertIn("好了", out)

    def test_plain_angle_brackets_in_prose_untouched(self):
        text = '条件 a < b 且 c > d，就这样'
        self.assertEqual(_strip_tool_blocks(text), text)
        self.assertEqual(parse_tool_calls(text), [])


class AnthropicXmlFormTest(unittest.TestCase):
    """Anthropic 式 XML 工具调用（2026-09-29 真机取证）。

    走 Anthropic 兼容端点的模型（mimo / doubao）会把工具调用吐成自己的原生
    格式，通篇没有 `]]`，老解析器一个字符都匹配不上 —— 结果是**工具没执行、
    整段 XML 连着提示词原样发进聊天**。日志里 84 条「发送 -> …<tool_call>」，
    83 条出自 mimo-v2.6-flash，而且每一条所在的那一轮 `工具=-`。

    下面的样本逐字取自清酒瓶子的私聊会话文件
    （agents/qq/sessions/private_546587874.jsonl）。

    ⚠️ 样本里出现的 `skill=anima_2` 是**当时真实发出来的内容**，`anima_2` 这个
    渠道后来已删除。**别把它「顺手改成 anima」**——一改就不再是逐字取证，这些
    用例是靠「原文长什么样」立住的，不是靠渠道名。
    """

    def test_real_leaked_generate_image(self):
        text = ('队列空着 跑一张<tool_call><function=generate_image>'
                '<parameter=prompt>anime illustration, young woman with pink '
                'twintails, masterpiece</parameter>'
                '<parameter=skill>anima_2</parameter></function></tool_call>')
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "generate_image")
        self.assertEqual(calls[0]["args"]["skill"], "anima_2")
        self.assertTrue(
            calls[0]["args"]["prompt"].startswith("anime illustration"))

    def test_real_leaked_multi_param_chinese(self):
        text = ('这轮就是二采画的<tool_call><function=memory_save>'
                '<parameter=content>用户生图默认偏好：使用 anima_2（双底模二采）'
                '作为默认生图渠道，除非另有指定。</parameter>'
                '<parameter=tags>生图,偏好,anima_2</parameter>'
                '<parameter=note>用户表示自己默认二采</parameter>'
                '</function></tool_call>')
        args = parse_tool_calls(text)[0]["args"]
        self.assertEqual(args["tags"], "生图,偏好,anima_2")
        self.assertEqual(args["note"], "用户表示自己默认二采")
        self.assertIn("anima_2", args["content"])

    def test_real_leaked_no_parameter(self):
        calls = parse_tool_calls(
            '<tool_call><function=memory_list></function></tool_call>')
        self.assertEqual(calls, [{"name": "memory_list", "args": {}}])

    def test_stray_quote_after_parameter_name(self):
        # 实测畸形：<parameter=nums">50</parameter>
        text = ('笑死 这图给你供上了<tool_call><function=send_sticker>'
                '<parameter=nums">50</parameter></function></tool_call>')
        self.assertEqual(parse_tool_calls(text)[0]["args"], {"nums": "50"})

    def test_canonical_invoke_form(self):
        text = ('<tool_call><invoke name="generate_image">'
                '<parameter name="prompt">a cat</parameter></invoke></tool_call>')
        self.assertEqual(
            parse_tool_calls(text),
            [{"name": "generate_image", "args": {"prompt": "a cat"}}])

    def test_name_case_normalized(self):
        calls = parse_tool_calls('<function=GENERATE_IMAGE>'
                                 '<parameter=prompt>x</parameter></function>')
        self.assertEqual(calls[0]["name"], "generate_image")

    def test_bare_function_without_wrapper(self):
        # 不带 <tool_call> 壳也要认（两种都实测出现过）
        calls = parse_tool_calls('<function=generate_image>'
                                 '<parameter=prompt>x</parameter></function>')
        self.assertEqual(calls[0]["args"], {"prompt": "x"})

    def test_multiple_calls_keep_order(self):
        text = ('先查后画<tool_call><function=queue_status></function></tool_call>'
                '<tool_call><function=generate_image><parameter=prompt>x'
                '</parameter></function></tool_call>')
        self.assertEqual([c["name"] for c in parse_tool_calls(text)],
                         ["queue_status", "generate_image"])

    def test_strip_removes_xml_and_wrapper(self):
        text = ('队列空着 跑一张<tool_call><function=generate_image>'
                '<parameter=prompt>a very long prompt here</parameter>'
                '<parameter=skill>anima_2</parameter></function></tool_call>')
        self.assertEqual(_strip_tool_blocks(text).strip(), "队列空着 跑一张")

    def test_strip_leaves_no_xml_behind(self):
        """剥块之后一个字都不该剩 —— 剩了就是又一次「把源码发进群」。"""
        samples = [
            '<tool_call><function=memory_list></function></tool_call>',
            '前<tool_call><function=send_sticker><parameter=nums>3'
            '</parameter></function></tool_call>后',
            '<function=generate_image><parameter=prompt>x</parameter></function>',
        ]
        for text in samples:
            out = _strip_tool_blocks(text)
            for marker in ("tool_call", "function=", "parameter"):
                self.assertNotIn(marker, out,
                                 "%r 里还留着 %s" % (text, marker))

    def test_prose_angle_brackets_untouched(self):
        text = '条件 a < b 且 c > d，就这样'
        self.assertEqual(_strip_tool_blocks(text), text)
        self.assertEqual(parse_tool_calls(text), [])

    def test_mixed_families_in_one_reply(self):
        """两族混用（真实出现过：<tool_call> 壳套着 [[TOOL:...]]）。"""
        text = '<tool_call><TOOL:send_sticker]]{"nums": "11"}[[/TOOL]]'
        self.assertEqual(parse_tool_calls(text),
                         [{"name": "send_sticker", "args": {"nums": "11"}}])
        self.assertEqual(_strip_tool_blocks(text).strip(), '')


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
