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


class SoloConflictTest(unittest.TestCase):
    """`solo` 与「第二方身体部位入画」互斥。

    2026-10-11 用户三张实测图的根因：danbooru 官方角色计数规则里「一只手也算
    一个角色」，所以 `1girl, solo` 配「抓手腕」是自相矛盾的指令，模型只能
    三选一妥协（手变成她自己的 / 抓握整个消失 / 手畸形）。这些标签**库里都
    存在**，missing_tags 拦不住，只能查语义冲突。
    """

    @staticmethod
    def _script(scene, no="01"):
        return "## %s\n画面: %s\n气泡: 一\n" % (no, scene)

    def test_solo_plus_anothers_wrist(self):
        got = comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, holding another's wrist"))
        self.assertEqual([("01", "holding_another's_wrist")], got)

    def test_solo_plus_anothers_wrist_without_apostrophe(self):
        """实测模型会漏撇号写成 `another wrist`，不能因此漏检。"""
        got = comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, holding another wrist"))
        self.assertEqual([("01", "holding_another_wrist")], got)

    def test_solo_plus_pov_hands_underscore(self):
        got = comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, pov_hands"))
        self.assertEqual([("01", "pov_hands")], got)

    def test_solo_plus_disembodied_hand_reports_panel_no(self):
        got = comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, disembodied hand", no="02"))
        self.assertEqual([("02", "disembodied_hand")], got)

    def test_holding_own_wrist_is_allowed(self):
        """抓自己手腕的格子，`solo` 是对的——不能误伤。"""
        self.assertEqual([], comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, holding own wrist")))

    def test_solo_focus_with_other_is_allowed(self):
        self.assertEqual([], comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo focus, 1other, holding another's wrist")))

    def test_plain_solo_is_allowed(self):
        self.assertEqual([], comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, standing, arm at side")))

    def test_check_rejects_solo_conflict(self):
        ok, why = comic_agent.check(
            self._script("safe, 1girl, solo, holding another's wrist"), 1)
        self.assertFalse(ok)
        self.assertIn("solo focus", why)


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

    # ─── 轮次预算：查库段用完必须强制动笔 ─────────────────
    #
    # 2026-10-11 用户实测：两稿 24 轮全烧在 search_tags 上，一次正文都没交，
    # 下游报「只解析到 0 格」。

    def test_forced_write_round_closes_tools(self):
        """查库段用完要往对话里插「查库时间到」，逼它动笔。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = [
            '[[TOOL:search_tags]]{"query": "一"}',   # 1 查库
            '[[TOOL:search_tags]]{"query": "二"}',   # 2 查库（最后一个查库轮）
            script,                                  # 3 强制轮：正文
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=4)
        self.assertIn("画面", out)
        self.assertEqual(m.call_count, 3)
        third = m.call_args_list[2].args[0]
        self.assertTrue(any("查库时间到" in str(x.get("content") or "")
                            for x in third))

    def test_forced_round_does_not_execute_tools(self):
        """强制轮里模型再输出工具调用也**不能执行**——一执行就把这一轮又变回

        查库轮，永远收敛不了。
        """
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = [
            '[[TOOL:search_tags]]{"query": "一"}',   # 1 查库
            '[[TOOL:search_tags]]{"query": "二"}',   # 2 查库
            '[[TOOL:search_tags]]{"query": "三"}',   # 3 强制轮仍查库 → 丢弃
            script,                                  # 4 强制轮：正文
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies), \
                mock.patch.object(comic_agent, "_run_tool",
                                  return_value="（假结果）") as t:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=4)
        self.assertIn("画面", out)
        self.assertEqual(t.call_count, 2)        # 只有前两轮真的执行了工具

    def test_exhausted_rounds_returns_process_draft(self):
        """到顶也没交出正文时，中间轮的半截稿要兜住，不能返回空串。

        原版必然返回空串——唯一给兜底变量赋值的就是「无工具调用」那个分支，
        而那个分支本身就是 return，所以模块文档承诺的兜底从没生效过。
        """
        half = ('## 01\n画面: 1girl, solo\n气泡: 一\n'
                '[[TOOL:search_tags]]{"query": "眼泪"}')
        only_tool = '[[TOOL:search_tags]]{"query": "眼泪"}'
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=[half, only_tool, only_tool, only_tool]):
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=4)
        self.assertIn("画面", out)

    def test_nudged_body_is_kept_as_fallback(self):
        """第 1 轮被打回的那份稿子要留作兜底。

        模型可能被打了回之后就一直查库、再也不交稿——那时宁可交一份「标签没
        验过」的稿子（下游还有出口校验和人工确认），也不要交空白。
        """
        first = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        only_tool = '[[TOOL:search_tags]]{"query": "眼泪"}'
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=[first, only_tool, only_tool, only_tool]):
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=4)
        self.assertIn("画面", out)

    def test_tool_result_carries_round_budget(self):
        """回灌工具结果时要报剩余轮数——预算得让模型自己能算。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = ['[[TOOL:search_tags]]{"query": "眼泪"}', script]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m:
            comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                   max_rounds=5)
        second = m.call_args_list[1].args[0]
        fed = " ".join(str(x.get("content") or "") for x in second)
        self.assertIn("还剩", fed)
        self.assertIn("强制", fed)


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

    def test_empty_draft_retries_with_targeted_prompt(self):
        """空正文不能和「有稿但标签不对」走同一条回炉路。

        空正文回炉若带一条**空的** assistant 消息，模型看不出自己错在哪（它
        眼里那就是"上一份稿子"），于是接着查库。这一路要用专门的话点明「你没
        交剧本」。
        """
        good = "# 标题：x\n\n## 01\n画面: 1girl, solo\n气泡: 一\n"
        with mock.patch.object(comic_agent, "run_rounds",
                               side_effect=["", good]) as m:
            out = comic_agent.write_script("x", panels=1, tries=2)
        self.assertIn("画面", out)
        second = m.call_args_list[1].args[0]
        self.assertTrue(any("没有交剧本" in str(x.get("content") or "")
                            for x in second))
        self.assertFalse(any(x.get("role") == "assistant" and not x.get("content")
                             for x in second))

    def test_all_empty_raises(self):
        """一稿都没写出正文就报错——返回空串会让调用方落一个空 script.md。"""
        with mock.patch.object(comic_agent, "run_rounds", return_value=""):
            with self.assertRaises(RuntimeError):
                comic_agent.write_script("x", panels=1, tries=2)

    def test_empty_then_keeps_earlier_draft(self):
        """后一稿空、前一稿有内容：最后一稿不能被空正文覆盖掉。"""
        bad = "# 标题：x\n\n## 01\n画面: 1girl, solo, arm grab\n气泡: 一\n"
        with mock.patch.object(comic_agent, "run_rounds",
                               side_effect=[bad, ""]):
            out = comic_agent.write_script("x", panels=1, tries=2)
        self.assertIn("[警告]", out)
        _, panels = comic.parse_script_text(out)
        self.assertEqual(len(panels), 1)
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
        # 真名（实测在库）：抓别人手腕的正解 + 第一人称手 + 无身体的手
        for real in ("holding another's wrist", "pov hands", "disembodied hand"):
            self.assertIn(real, p)
        # 假名（实测不在库）：必须出现在黑名单里
        for fake in ("sfw", "arm grab", "male hand", "medium shot", "soft lighting"):
            self.assertIn(fake, p)

    def test_teaches_solo_focus_rule(self):
        """人数规则必须写进提示词：出现第二方身体部位时不许写 `solo`。

        2026-10-11：`1girl, solo` 配「抓手腕」是自相矛盾的指令（danbooru
        官方计数规则里一只手也算一个角色），模型只能三选一妥协。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("solo focus", p)
        self.assertIn("1other", p)
        # `hand around wrist` 只能出现在「反例 · 别用」的语境里
        self.assertIn("hand around wrist", p)
        self.assertIn("她自己抓自己", p)

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

    def test_teaches_round_budget(self):
        """必须把轮次预算告诉它：查库段用完会强制动笔（工具关闭）。

        2026-10-11 用户实测：两稿 24 轮全烧在 search_tags 上，一次正文都没交。
        提示词只写「先查完再写」没用——它不知道自己在烧最后的机会。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("轮次预算", p)
        self.assertIn("不要查第二遍", p)
        self.assertIn("15~25", p)             # 要教它一轮批量查，别一轮 5 个
        # 强制动笔那条指令本身
        self.assertIn("查库时间到", comic_agent._FORCE_WRITE)
        self.assertIn("工具已经关闭", comic_agent._FORCE_WRITE)

    def test_fake_alias_marked_missing(self):
        """`grabbing another's wrist` / `wrist grab` 实测**不在**本地库里。

        上一版提示词把它们当可用别名推荐（写错了），模型照着查会落空。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("在本库里**都不存在**", p)


if __name__ == "__main__":
    unittest.main()
