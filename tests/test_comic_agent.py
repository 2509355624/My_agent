# -*- coding: utf-8 -*-
"""编剧 Agent 测试：标签归一 / 查库判据 / 备选记录归位 / 查库循环。

2026-10-11 加。背景：用户发现漫画 `画面:` 写成了自然语言
（`male hand grabbing her wrist from behind`）导致多手与错位，要求把编剧
从「单次 API 盲写」换成「Agent 查库写」。

这份测试钉死四件事：
  1. 标签归一（权重语法 / 转义括号 / artist 的 @ / 大小写）;
  2. **代码判据**：库里查不到的标签必须被点名，官方前缀标签必须被豁免
     —— 这是提示词约束之外唯一能兜住「模型蒙一个假 tag」的防线；
  3. 备选行被抽走并挪到剧本头部，且解析后**不污染任何字段**
     （parse_script_text 会把未知行当续行拼进上一个字段，这是最易踩的坑）;
  4. agent 循环：工具结果回灌、不收敛时兜底交出最后一稿。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_agent


class NormTagTest(unittest.TestCase):
    """剧本里的标签文本 → 带空格的规范形态（**不转下划线**，见 _norm_tag 注释）。"""

    def test_plain_lower_and_spaces(self):
        self.assertEqual(comic_agent._norm_tag("School Uniform"), "school uniform")

    def test_weight_syntax(self):
        self.assertEqual(comic_agent._norm_tag("(chibi:2)"), "chibi")

    def test_escaped_parens(self):
        self.assertEqual(comic_agent._norm_tag("vertin \\(reverse:1999\\)"),
                         "vertin (reverse:1999)")

    def test_artist_at_prefix_stripped(self):
        self.assertEqual(comic_agent._norm_tag("@big chungus"), "big chungus")

    def test_series_with_colon_untouched(self):
        # 不带括号的系列名，冒号不是权重语法
        self.assertEqual(comic_agent._norm_tag("reverse:1999"), "reverse:1999")

    def test_db_key_underscores(self):
        self.assertEqual(comic_agent._db_key("hand around wrist"), "hand_around_wrist")


class MissingTagsTest(unittest.TestCase):
    """代码判据：逐个标签查库。"""

    def test_real_tags_pass(self):
        text = ("## 01\n画面: 1girl, solo, school uniform, hand around wrist, "
                "close-up\n气泡: 一\n")
        self.assertEqual(comic_agent.missing_tags(text), [])

    def test_fake_tag_caught(self):
        # arm_grab 看着很像对的，实测库里没有（真名是 hand_grab）
        text = "## 01\n画面: 1girl, solo, arm grab\n气泡: 一\n"
        self.assertEqual(comic_agent.missing_tags(text), [("01", "arm_grab")])

    def test_prefix_tags_exempt(self):
        # 官方前缀标签不在 booru 词库里，规范第 2 节明确豁免
        text = ("## 01\n画面: masterpiece, best quality, safe, 1girl, solo\n"
                "气泡: 一\n")
        self.assertEqual(comic_agent.missing_tags(text), [])

    def test_year_and_score_exempt(self):
        text = "## 01\n画面: year 2025, score_7, 1girl\n气泡: 一\n"
        self.assertEqual(comic_agent.missing_tags(text), [])

    def test_weight_wrapped_real_tag_passes(self):
        text = "## 01\n画面: 1girl, (close-up:1.5)\n气泡: 一\n"
        self.assertEqual(comic_agent.missing_tags(text), [])

    def test_reports_panel_number(self):
        text = ("## 01\n画面: 1girl, solo\n气泡: 一\n\n"
                "## 02\n画面: 1girl, solo, medium shot\n气泡: 二\n")
        self.assertEqual(comic_agent.missing_tags(text), [("02", "medium_shot")])


class NoteExtractTest(unittest.TestCase):
    """备选记录必须抽走、挪到头部，且不能污染剧本字段。"""

    RAW = ("# 标题：公交站\n\n## 01\n画面: 1girl, solo, hand grab\n气泡: 一\n"
           "备选: 选了 hand grab，另有 hand around wrist\n\n"
           "## 02\n画面: 1girl, solo\n气泡: 二\n")

    def test_extract_removes_note_lines(self):
        clean, notes = comic_agent._extract_notes(self.RAW)
        self.assertEqual(len(notes), 1)
        self.assertIn("hand around wrist", notes[0])
        self.assertNotIn("备选", clean)

    def test_place_notes_moves_to_header(self):
        clean, notes = comic_agent._extract_notes(self.RAW)
        out = comic_agent._place_notes(clean, notes)
        self.assertLess(out.index("[备选]"), out.index("## 01"))

    def test_placed_notes_do_not_pollute_fields(self):
        clean, notes = comic_agent._extract_notes(self.RAW)
        out = comic_agent._place_notes(clean, notes)
        _, panels = comic.parse_script_text(out)
        self.assertEqual(len(panels), 2)
        self.assertEqual(panels[0]["scene"], "1girl, solo, hand grab")
        self.assertEqual(panels[0]["bubble"], "一")
        self.assertEqual(panels[0]["label"], "")
        self.assertEqual(panels[1]["bubble"], "二")
        self.assertEqual(panels[1]["label"], "")

    def test_warning_line_keeps_own_prefix(self):
        out = comic_agent._place_notes("## 01\n画面: 1girl\n气泡: 一\n",
                                       ["[警告] 没过"])
        self.assertIn("[警告] 没过", out)
        # 不能叠成 `[备选] [警告] …`（实测踩过）
        self.assertNotIn("[备选] [警告]", out)

    def test_duplicate_notes_deduped(self):
        """同一格把同样的备选写两遍是常态，去重后只留一条。"""
        out = comic_agent._place_notes("## 01\n画面: 1girl\n",
                                       ["选了 A，另有 B", "选了 A，另有 B"])
        self.assertEqual(out.count("[备选]"), 1)

    def test_no_notes_is_passthrough(self):
        raw = "## 01\n画面: 1girl\n"
        self.assertEqual(comic_agent._place_notes(raw, []), raw)


class PreambleTest(unittest.TestCase):
    """剧本开头的碎碎念必须砍掉（模型交卷前的自言自语）。"""

    def test_strips_rambling_before_header(self):
        raw = ("剧本已足够：用 grabbing another's arm（抓他人手臂），"
               "配 from side 表现拉扯。\n\n"
               "# 标题：别闹了\n\n## 01\n画面: 1girl\n气泡: 一\n")
        out = comic_agent._strip_preamble(raw)
        self.assertTrue(out.startswith("# 标题：别闹了"))
        self.assertNotIn("剧本已足够", out)

    def test_keeps_text_that_starts_with_panel(self):
        raw = "## 01\n画面: 1girl"
        self.assertEqual(comic_agent._strip_preamble(raw), raw)

    def test_never_returns_empty_on_normal_script(self):
        raw = "# 标题：x\n\n## 01\n画面: 1girl\n气泡: 一\n"
        _, panels = comic.parse_script_text(comic_agent._strip_preamble(raw))
        self.assertEqual(len(panels), 1)


class CheckTest(unittest.TestCase):
    """契约校验 + 查库校验的合并判据。"""

    def test_accepts_good_script(self):
        ok, why = comic_agent.check(
            "## 01\n画面: 1girl, solo, hand grab\n气泡: 一\n", 1)
        self.assertTrue(ok, why)

    def test_rejects_cjk_in_scene(self):
        ok, why = comic_agent.check("## 01\n画面: 1girl, 中文\n气泡: 一\n", 1)
        self.assertFalse(ok)
        self.assertIn("中文", why)

    def test_rejects_missing_tag_with_name(self):
        ok, why = comic_agent.check(
            "## 01\n画面: 1girl, solo, fake tag\n气泡: 一\n", 1)
        self.assertFalse(ok)
        self.assertIn("fake_tag", why)

    def test_rejects_wrong_panel_count(self):
        ok, why = comic_agent.check(
            "## 01\n画面: 1girl, solo\n气泡: 一\n", 2)
        self.assertFalse(ok)


class RunRoundsTest(unittest.TestCase):
    """agent 循环：工具调用 → 结果回灌 → 收尾轮交出正文。"""

    def test_tool_round_then_script(self):
        replies = [
            '[[TOOL:search_tags]]{"query": "抓手"}',
            '## 01\n画面: 1girl, solo, hand grab\n气泡: 一\n',
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m:
            msgs = [{"role": "user", "content": "写个剧本"}]
            out = comic_agent.run_rounds(msgs, max_rounds=5)
        self.assertIn("画面", out)
        # 第二次调用时，工具结果必须已经回灌进 messages
        second_msgs = m.call_args_list[1].args[0]
        self.assertTrue(any("工具结果" in str(x.get("content") or "")
                            for x in second_msgs))

    def test_unknown_tool_is_reported_not_crashed(self):
        replies = [
            '[[TOOL:delete_file]]{"path": "x"}',
            '## 01\n画面: 1girl, solo\n气泡: 一\n',
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=5)
        self.assertIn("画面", out)
        self.assertTrue(any("没有这个工具" in str(x.get("content") or "")
                            for x in m.call_args_list[1].args[0]))

    def test_llm_failure_returns_last_text(self):
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=RuntimeError("boom")):
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=3)
        self.assertEqual(out, "")

    def test_progress_callback_called(self):
        seen = []
        replies = [
            '[[TOOL:search_tags]]{"query": "眼泪"}',
            '## 01\n画面: 1girl, solo, tears\n气泡: 一\n',
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies):
            comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                   max_rounds=5, progress=seen.append)
        self.assertTrue(any("search_tags" in s for s in seen))

    def test_first_round_without_tools_is_nudged(self):
        """第一轮直接交正文必须被打回——实测提示词挡不住，模型觉得自己知道。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = [
            script,                                    # 第 1 轮：不查库直接交卷
            '[[TOOL:search_tags]]{"query": "眼泪"}',     # 被打回后去查
            script,                                    # 收尾
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=5)
        self.assertIn("画面", out)
        second = m.call_args_list[1].args[0]
        self.assertTrue(any("还没有查过库" in str(x.get("content") or "")
                            for x in second))

    def test_nudge_happens_only_once(self):
        """打回只来一次：模型仍不查就认了，不能无限打回烧 token。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        with mock.patch.object(comic_agent.llm, "call_llm",
                               return_value=script) as m:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=5)
        self.assertIn("画面", out)
        self.assertEqual(m.call_count, 2)


class WriteScriptTest(unittest.TestCase):
    """对外入口：合格即返回（含头部备选），不合格回炉、最后一次容忍。"""

    def test_returns_script_with_note_header(self):
        good = ("# 标题：公交站\n\n## 01\n画面: 1girl, solo, hand grab\n"
                "气泡: 一\n备选: 选了 hand grab，另有 hand around wrist\n")
        with mock.patch.object(comic_agent, "run_rounds", return_value=good):
            out = comic_agent.write_script("公交站被抓包", panels=1)
        self.assertIn("[备选]", out)
        self.assertLess(out.index("[备选]"), out.index("## 01"))
        _, panels = comic.parse_script_text(out)
        self.assertEqual(len(panels), 1)
        self.assertEqual(panels[0]["scene"], "1girl, solo, hand grab")

    def test_retry_then_pass(self):
        bad = ("# 标题：x\n\n## 01\n画面: 1girl, solo, arm grab\n气泡: 一\n")
        good = ("# 标题：x\n\n## 01\n画面: 1girl, solo, hand grab\n气泡: 一\n")
        with mock.patch.object(comic_agent, "run_rounds",
                               side_effect=[bad, good]) as m:
            out = comic_agent.write_script("x", panels=1, tries=2)
        self.assertEqual(m.call_count, 2)
        self.assertIn("hand grab", out)
        # 回炉那一轮必须带上错因，且**不带**上一轮的工具中间结果
        second_msgs = m.call_args_list[1].args[0]
        self.assertTrue(any("不合格" in str(x.get("content") or "")
                            for x in second_msgs))

    def test_last_try_tolerated_with_warning(self):
        bad = ("# 标题：x\n\n## 01\n画面: 1girl, solo, arm grab\n气泡: 一\n")
        with mock.patch.object(comic_agent, "run_rounds", return_value=bad):
            out = comic_agent.write_script("x", panels=1, tries=1)
        self.assertIn("[警告]", out)
        # 警告必须在头部，不能落到格内污染台词
        _, panels = comic.parse_script_text(out)
        self.assertEqual(panels[0]["bubble"], "一")

    def test_empty_brief_raises(self):
        with self.assertRaises(ValueError):
            comic_agent.write_script("   ")

    def test_provider_defaults_to_current_not_chain_head(self):
        """留空要落到「全局生效的那家」，不能让 call_llm 从降级链头开跑。

        2026-10-11 实跑逮到：provider 传 None 时日志是 `volc / deepseek-v4-flash`
        —— llm.call_llm 在 provider 为空时直接走降级链，`.env` 的
        LLM_PROVIDER=deepseek 被绕过。
        """
        good = "# 标题：x\n\n## 01\n画面: 1girl, solo\n气泡: 一\n"
        with mock.patch.object(comic_agent, "run_rounds",
                               return_value=good) as m, \
                mock.patch.object(comic_agent.llm, "CURRENT_PROVIDER",
                                  "deepseek"):
            comic_agent.write_script("x", panels=1)
        self.assertEqual(m.call_args.kwargs.get("provider"), "deepseek")

    def test_explicit_provider_wins(self):
        good = "# 标题：x\n\n## 01\n画面: 1girl, solo\n气泡: 一\n"
        with mock.patch.object(comic_agent, "run_rounds",
                               return_value=good) as m:
            comic_agent.write_script("x", panels=1, provider="volc",
                                     model="m1")
        self.assertEqual(m.call_args.kwargs.get("provider"), "volc")
        self.assertEqual(m.call_args.kwargs.get("model"), "m1")


class PromptTest(unittest.TestCase):
    """system prompt 必须带规范全文与真实标签示例。"""

    def test_contains_full_spec_and_no_placeholder(self):
        p = comic_agent._system_prompt(3)
        self.assertIn("规范全文开始", p)
        self.assertIn("Anima 标签规范", p)
        self.assertNotIn("__SPEC__", p)
        self.assertNotIn("__PANELS__", p)
        self.assertNotIn("__MAX_BUBBLE__", p)

    def test_lists_real_hand_tags_and_fake_ones(self):
        p = comic_agent._system_prompt(3)
        # 真名（实测在库）：用作「对」的例子
        for real in ("disembodied arm", "hand grab", "hand around wrist"):
            self.assertIn(real, p)
        # 假名（实测不在库）：必须出现在黑名单里
        for fake in ("sfw", "arm grab", "medium shot", "soft lighting"):
            self.assertIn(fake, p)

    def test_panel_count_injected(self):
        self.assertIn("正好 7 格", comic_agent._system_prompt(7))

    def test_tool_call_format_is_taught(self):
        """必须写明 [[TOOL:...]]{...} 格式。

        2026-10-11 实跑踩到：提示词里只说了「先查库」没说**怎么调**，模型
        输出了 29 行 `search_tags: 抓手腕` 这样的纯文本（不是工具调用），
        parse_tool_calls 一个都没解析到，整轮白烧。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn('[[TOOL:search_tags]]{"query"', p)
        self.assertIn("[[TOOL:read_file]]", p)
        self.assertIn("[[TOOL:web_search]]", p)
        self.assertIn("查库只能写成上面的", p)


if __name__ == "__main__":
    unittest.main()
