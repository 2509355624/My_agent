#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直达生图管道（app/direct_gen.py）测试。

核心契约（2026-10-05 渠道收归代码版）：
- /菜单、裸 @、裸名字 → 菜单常量，零 LLM
- 渠道词打头 → **代码正则**定渠道（档位最核心：画风打错/没打 → 该档默认
  clear），LLM 只扩写描述
- 裸 @ + 描述 / 画图动词 → 一次转译（LLM 顺带判渠道）
- 引用正文 + 只打渠道词 → 引用内容当描述，锁定渠道扩写
- 引用回执 +「再来一张」→ 同提示词换种子重跑，零转译
- 引用图 + 意见 → 改图管道；意见不是修改请求（skip）→ @ 轮回菜单、
  关键词轮静默
- @ 轮兜底 = 菜单；关键词轮兜底 = 静默（None）——止住菜单刷屏
"""
import unittest
from unittest import mock

from app import direct_gen, image_jobs
from app.direct_gen import MENU_TEXT
from app.tools.normal import generate_image as gi

RECEIPT = image_jobs.RECEIPT_SENT_MARK + "回执"


class ChannelParseTest(unittest.TestCase):
    """渠道解析收归代码：判据必须确定性，用群聊实录出题。"""

    def test_tier_and_style(self):
        for text, skill, desc in (
                ("三档 clear 初音未来", "hd_3_clear", "初音未来"),
                ("三档 soft 初音未来", "hd_3_soft", "初音未来"),
                ("二档 gloss 女骑士", "hd_2_gloss", "女骑士"),
                ("快档 一只柴犬在草地上", "hd_fast_clear", "一只柴犬在草地上"),
                ("默认初音未来", "anima_clear", "初音未来"),   # 无分隔符
                ("默认，纳西妲", "anima_clear", "纳西妲"),
                ("默认 gloss 一个女孩", "anima_gloss", "一个女孩"),
                ("gloss 一个女孩", "anima_gloss", "一个女孩"),  # 只打画风
                ("三档 猫", "hd_3_clear", "猫"),               # 画风没打
                ("一档curvy 初音未来", "hd_fast_curvy", "初音未来"),  # 连写
                ("nai 1girl, masterpiece", "nai", "1girl, masterpiece")):
            with self.subTest(text=text):
                got_skill, got_desc = direct_gen._parse_channel(text)
                self.assertEqual((got_skill, got_desc), (skill, desc))

    def test_fixed_channel_words(self):
        # 用户点名的固定渠道也要代码直判，不劳 LLM（2026-10-05）
        for text, skill, desc in (
                ("sd 一只猫", "image_gen_v1", "一只猫"),
                ("krea2 一个女孩", "krea2", "一个女孩"),
                ("qwen 写实街拍", "qwen_image_v1", "写实街拍"),
                ("nffa 插画少女", "nffa", "插画少女"),
                ("这个猪 跑 nai", "nai", "这个猪"),   # 渠道词不限位置+动词残渣
                ("nai 伊藤润二画风", "nai", "伊藤润二画风")):
            with self.subTest(text=text):
                self.assertEqual(direct_gen._parse_channel(text),
                                 (skill, desc))

    def test_fixed_word_inside_english_prompt_is_ignored(self):
        # 档位在场时固定渠道词不参与（英文提示词里撞词不误判）
        self.assertEqual(
            direct_gen._parse_channel("三档 1girl, solo, sd style"),
            ("hd_3_clear", "1girl, solo, sd style"))

    def test_typo_style_falls_back(self):
        # 画风词打错：贴得回来（glss→gloss）就修正；贴不回来按档位默认 clear，
        # 原词留在描述里不丢。
        self.assertEqual(direct_gen._parse_channel("三档,glss,初音未来"),
                         ("hd_3_gloss", "初音未来"))
        skill, desc = direct_gen._parse_channel("三档,miku,初音未来")
        self.assertEqual(skill, "hd_3_clear")
        self.assertIn("miku", desc)

    def test_no_channel_word_returns_none(self):
        text = "初音未来，全身照，anime，正身平齐视角"
        self.assertEqual(direct_gen._parse_channel(text), (None, text))

    def test_own_name_expansion_is_stripped(self):
        # 群实录 2026-10-05 01:07：文字 @ 的昵称带括号扩展，顶着名字渠道词
        # 永远匹配不上。
        stripped = direct_gen._strip_own_names(
            "@大大怪（生图机器人，贼拉快，种类多） 三档 soft 初音未来")
        self.assertEqual(direct_gen._parse_channel(stripped),
                         ("hd_3_soft", "初音未来"))


class MenuAndGateTest(unittest.TestCase):
    def test_menu_keywords_return_constant(self):
        for t in ("菜单", "/菜单", "！菜单", "help", "帮助", "指令",
                  ""):                      # 裸 @/裸名字（剥完啥都不剩）
            with self.subTest(t=t):
                self.assertEqual(direct_gen.decide(t, [], False),
                                 direct_gen.MENU_TEXT)

    def test_attribution_prefix_is_stripped_before_menu_match(self):
        self.assertEqual(
            direct_gen.decide("胡桃桃：菜单", [], False),
            direct_gen.MENU_TEXT)

    def test_voluntary_turns_never_taken_over(self):
        self.assertIsNone(direct_gen.decide("画一只猫", [], True))

    def test_at_chatter_tries_translate_then_menu(self):
        # @ 轮兜底 = 菜单：先试一次转译，模型说跟画图无关 → 菜单
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"skill": "", "prompt": ""}'
                               ) as m_llm:
            out = direct_gen.decide("今天天气不错", [], False, at_me=True)
        self.assertIn(MENU_TEXT, out)
        self.assertEqual(m_llm.call_count, 1)

    def test_keyword_chatter_is_silent(self):
        # 关键词命中但纯闲聊（小小怪的聊天里提到名字）→ 静默不理，零 LLM
        with mock.patch.object(direct_gen.llm, "call_llm") as m_llm:
            out = direct_gen.decide("感觉这下大大怪比小小怪提词准一倍了",
                                    [], False, at_me=False)
        self.assertIsNone(out)
        m_llm.assert_not_called()


class DirectEnqueueTest(unittest.TestCase):
    def _decide(self, llm_reply, text="画一只戴帽子的橘猫", history=None,
                at_me=True, quoted=""):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value=llm_reply) as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=quoted), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, history or [], False, at_me=at_me)
        return out, m_llm, m_gen

    def test_translated_json_enqueues_directly(self):
        out, m_llm, m_gen = self._decide(
            '{"skill": "hd_2_gloss", "prompt": "1girl, hat"}')
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, hat", skill="hd_2_gloss",
                                      _skip_confirm=True)
        self.assertEqual(m_llm.call_count, 1)
        self.assertEqual(m_llm.call_args[0][0][0]["role"], "user")

    def test_recent_history_filters_menu_and_receipts(self):
        # 最近10条上下文要滤掉菜单和回执（2026-10-05 用户点名），指令本身留着
        hist = [{"role": "assistant", "content": MENU_TEXT},
                {"role": "assistant", "content": "任务已提交，正在画了。"},
                {"role": "user", "content": "三档 clear 初音未来"},
                {"role": "assistant",
                 "content": "[直达生图] hd_3_clear：miku"},
                {"role": "user", "content": "那只猫真可爱"}]
        out, m_llm, _ = self._decide(
            '{"skill": "anima_clear", "prompt": "cat"}',
            text="把它画出来", history=hist)
        self.assertEqual(out, "")
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("那只猫真可爱", sent)
        self.assertIn("三档 clear 初音未来", sent)
        self.assertNotIn("🎨", sent)
        self.assertNotIn("任务已提交", sent)

    def test_unknown_skill_falls_back_to_default(self):
        out, _, m_gen = self._decide(
            '{"skill": "gpt4o-image", "prompt": "cat"}')
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_args.kwargs["skill"],
                         direct_gen._DEFAULT_SKILL)

    def test_bare_at_description_runs_default(self):
        # 群实录 2026-10-05 01:07：裸 @ + 描述没有动词，之前被回菜单，
        # 现在默认档直跑
        out, m_llm, m_gen = self._decide(
            '{"skill": "anima_clear", "prompt": "hatsune miku"}',
            text="初音未来，全身照，anime，正身平齐视角")
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("hatsune miku", skill="anima_clear",
                                      _skip_confirm=True)

    def test_channel_lead_locks_skill_and_skips_channel_judgement(self):
        # 渠道词打头 → 代码定渠道，LLM 只扩写
        out, m_llm, m_gen = self._decide(
            '{"skill": "hd_3_clear", "prompt": "hatsune miku"}',
            text="三档 clear 初音未来")
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("hatsune miku", skill="hd_3_clear",
                                      _skip_confirm=True)
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("渠道已定：hd_3_clear", sent)
        self.assertIn("初音未来", sent)

    def test_typo_style_resolves_in_full_pipeline(self):
        out, _, m_gen = self._decide(
            '{"skill": "hd_3_gloss", "prompt": "miku"}',
            text="三档,glss,初音未来")
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_args.kwargs["skill"], "hd_3_gloss")

    def test_non_image_verdict_returns_menu(self):
        out, m_llm, m_gen = self._decide('{"skill": "", "prompt": ""}')
        self.assertIn(MENU_TEXT, out)
        m_gen.assert_not_called()

    def test_broken_json_returns_menu_not_agent(self):
        for bad in ("我不是 JSON", '{"skill": "anima_clear"',
                    '前置废话 {"skill": ok}'):
            with self.subTest(bad=bad):
                out, m_llm, m_gen = self._decide(bad)
                self.assertIn(MENU_TEXT, out)
                m_gen.assert_not_called()

    def test_llm_failure_at_vs_keyword(self):
        # @ 轮失败 → 提示 + 菜单；关键词轮失败 → 静默
        with mock.patch.object(direct_gen.llm, "call_llm",
                               side_effect=RuntimeError("429")), \
             mock.patch.object(gi, "_generate_image") as m_gen:
            out_at = direct_gen.decide("画一只猫", [], False, at_me=True)
            out_kw = direct_gen.decide("画一只猫", [], False, at_me=False)
        self.assertIn(MENU_TEXT, out_at)
        self.assertIsNone(out_kw)
        m_gen.assert_not_called()

    def test_tool_error_is_humanized(self):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"skill": "nai", "prompt": "x"}'), \
             mock.patch.object(gi, "_generate_image",
                               return_value="错误：NAI 仅支持 QQ。"
                                            "直接告诉对方现在用不了。"):
            out = direct_gen.decide("nai 画一只猫", [], False)
        self.assertEqual(out, "错误：NAI 仅支持 QQ。")


class QuotedPromptTest(unittest.TestCase):
    """引用正文当提示词：引用一条带描述的消息 + 只发渠道词。"""

    def _decide(self, text, llm_reply, quoted, last_job=None):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value=llm_reply) as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=quoted), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            if last_job:
                direct_gen._remember_job("group_1", last_job["skill"],
                                         last_job["prompt"])
            out = direct_gen.decide(text, [], False, at_me=False)
            direct_gen._LAST_JOB.pop("group_1", None)
        return out, m_llm, m_gen

    def test_quoted_english_prompt_passes_through_without_llm(self):
        # 2026-10-05 用户场景：引用自己粘贴的英文提示词 +「快档 gloss」→
        # 原样入队零转译（之前要过一遍模型，可能改坏还烧钱）。
        out, m_llm, m_gen = self._decide(
            "快档 gloss", "x",
            quoted="1girl, solo, white dress, standing in a garden")
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with(
            "1girl, solo, white dress, standing in a garden",
            skill="hd_fast_gloss", _skip_confirm=True)

    def test_quoted_chinese_prompt_goes_through_translate(self):
        # 中文引用照旧走锁定渠道扩写
        out, m_llm, m_gen = self._decide(
            "三档",
            '{"skill": "hd_3_clear", "prompt": "1girl, hat"}',
            quoted="一个女孩在花园里")
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, hat", skill="hd_3_clear",
                                      _skip_confirm=True)
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("花园", sent)
        self.assertIn("渠道已定：hd_3_clear", sent)

    def test_quoted_reverse_reply_runs_directly(self):
        # 引用机器人的反推回复 + 渠道词 → 剥头直用 tag，零 LLM
        out, m_llm, m_gen = self._decide(
            "三档", "x",
            quoted=direct_gen._REVERSE_HEADER + "\n1girl, twintails, aqua hair")
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("1girl, twintails, aqua hair",
                                      skill="hd_3_clear", _skip_confirm=True)

    def test_quoted_menu_is_rejected(self):
        out, m_llm, m_gen = self._decide("三档", "x", quoted=MENU_TEXT)
        self.assertIn("引用", out)
        m_llm.assert_not_called()
        m_gen.assert_not_called()

    def test_quoted_receipt_redoes_last_job(self):
        # 引用生图回执 +「三档」→ 同提示词换种子重跑，零转译
        out, m_llm, m_gen = self._decide(
            "三档", "x",
            quoted="生图完成已发回：1/1 张（编号 HT-20261005-010329-595，"
                   "渠道 hd_3_curvy，seed 3357761196，耗时 62.3 秒）",
            last_job={"skill": "hd_3_curvy", "prompt": "miku, old"})
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("miku, old", skill="hd_3_clear",
                                      _skip_confirm=True)


class AgainTest(unittest.TestCase):
    def test_again_redoes_last_job_without_llm(self):
        with mock.patch.object(direct_gen.llm, "call_llm") as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            self.addCleanup(direct_gen._LAST_JOB.pop, "group_1", None)
            direct_gen._remember_job("group_1", "hd_3_clear", "miku, old")
            out = direct_gen.decide("再来一张", [], False, at_me=True)
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("miku, old", skill="hd_3_clear",
                                      _skip_confirm=True)


class RevisionPipelineTest(unittest.TestCase):
    """改图管道：引用图 → 英文 tag 反推（唯一事实来源）→ 修正调用 → 重跑。

    原提示词只在引用自家 HT 图（账本可查）时进场——2026-10-05 群实录：
    引用别人的图时上一轮原提示词把模型锚死，出图跟引用图毫无关系。
    """

    def _decide(self, text, llm_reply, seen="1girl, solo, blue hair",
                quoted="", at_me=False, last_job=None, lookup_row=None):
        with mock.patch.object(direct_gen, "llm") as m_llm_mod, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=quoted), \
             mock.patch("app.vision.describe", return_value=seen), \
             mock.patch.object(direct_gen.image_log, "lookup",
                               return_value=lookup_row), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            m_llm_mod.call_llm.return_value = llm_reply
            if last_job:
                direct_gen._remember_job("group_1", last_job["skill"],
                                         last_job["prompt"])
            out = direct_gen.decide(text, [{"role": "user",
                                            "content": "画个女孩"}],
                                    False, data_urls=["data:image/jpeg;base64,A"],
                                    at_me=at_me)
            direct_gen._LAST_JOB.pop("group_1", None)
        return out, m_llm_mod.call_llm, m_gen

    def test_revision_uses_tags_as_source_of_truth(self):
        out, m_llm, m_gen = self._decide(
            "手改成插兜",
            '{"skill": "anima_clear", "prompt": "1girl, hands in pockets"}')
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, hands in pockets",
                                      skill="anima_clear", _skip_confirm=True)
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("blue hair", sent)        # 英文 tag 反推进来了
        self.assertIn("手改成插兜", sent)        # 用户意见进来了
        self.assertNotIn("1girl, old", sent)    # 没有账本就不给原提示词

    def test_own_image_uses_logged_prompt(self):
        # 引用自家 HT 图：账本里的当时提示词最可信，进场当锚
        out, m_llm, _ = self._decide(
            "手改成插兜",
            '{"skill": "hd_3_curvy", "prompt": "miku, fixed"}',
            quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertEqual(out, "")
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("logged, miku", sent)
        self.assertIn("最可信", sent)

    def test_foreign_image_ignores_last_job(self):
        # 引用别人的图：上一轮任务的原提示词绝不进场（锚死事故的根因）
        out, m_llm, m_gen = self._decide(
            "手改成插兜",
            '{"skill": "anima_clear", "prompt": "1girl, fixed"}',
            last_job={"skill": "anima_clear", "prompt": "1girl, old"})
        self.assertEqual(out, "")
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertNotIn("1girl, old", sent)
        self.assertIn("忽略此项", sent)
        m_gen.assert_called_once()

    def test_bare_at_with_image_returns_reverse_text(self):
        # 2026-10-05 用户口径：引用图 + 只 @（没别的说）→ 反推提示词返回，
        # 不生成
        out, m_llm, m_gen = self._decide("", '{"skip": true}', at_me=True)
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("blue hair", out)
        m_llm.assert_not_called()
        m_gen.assert_not_called()

    def test_channel_with_image_generates_from_reverse_zero_llm(self):
        # 引用图 + 只打档位（「三档」）→ 反推后直接生成，零 LLM
        out, m_llm, m_gen = self._decide("三档", "x", at_me=True)
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("1girl, solo, blue hair",
                                      skill="hd_3_clear", _skip_confirm=True)

    def test_generic_i2i_filler_with_channel(self):
        # 引用图 +「快档 基于图片帮我生成」→ 空话不算意见，反推后直接生成
        out, m_llm, m_gen = self._decide(
            "快档 基于图片帮我生成", "x", at_me=True)
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("1girl, solo, blue hair",
                                      skill="hd_fast_clear",
                                      _skip_confirm=True)

    def test_praise_is_skipped_silently_on_keyword_round(self):
        # 群实录 2026-10-05 01:08：引用图 +「比大大怪快五秒左右」（夸奖）
        # → 关键词轮静默。
        out, m_llm, m_gen = self._decide(
            "比大大怪快五秒左右", '{"skip": true}')
        self.assertIsNone(out)
        m_gen.assert_not_called()

    def test_praise_on_at_round_returns_reverse_text(self):
        # @ 轮意见不是修改请求 → 按用户口径回反推文本（不刷菜单）
        out, _, m_gen = self._decide("画得真好", '{"skip": true}', at_me=True)
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        m_gen.assert_not_called()

    def test_revision_without_any_context_guides_user(self):
        out, m_llm, m_gen = self._decide("手改成插兜", "x", seen="")
        self.assertIn("没认出引用的图", out)
        m_gen.assert_not_called()

    def test_revision_fails_translating_returns_error(self):
        out, _, m_gen = self._decide("手改成插兜", "不是 JSON")
        self.assertIn("没解析出来", out)
        m_gen.assert_not_called()


class EnglishDirectTest(unittest.TestCase):
    """档位 + 英文提示词 → 原样直通零转译（用户贴的就是最终 prompt）。"""

    def test_english_prompt_passes_through_without_llm(self):
        text = "快档 gloss 1girl, solo, blue hair, classroom"
        with mock.patch.object(direct_gen.llm, "call_llm") as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False, at_me=True)
            direct_gen._LAST_JOB.pop("group_1", None)
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("1girl, solo, blue hair, classroom",
                                      skill="hd_fast_gloss",
                                      _skip_confirm=True)

    def test_chinese_desc_still_translated(self):
        # 中文描述照旧走扩写，别把直通判据写宽了
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"skill": "hd_fast_clear", '
                                            '"prompt": "shiba"}') as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide("快档 一只柴犬在草地上", [], False)
            direct_gen._LAST_JOB.pop("group_1", None)
        self.assertEqual(out, "")
        self.assertEqual(m_llm.call_count, 1)
        m_gen.assert_called_once_with("shiba", skill="hd_fast_clear",
                                      _skip_confirm=True)


class SkipConfirmTest(unittest.TestCase):
    def test_intercept_skips_when_told(self):
        from app import confirm_gate, qq_api
        with mock.patch.object(qq_api, "current_context",
                               return_value=("group", "123")), \
             mock.patch.object(qq_api, "current_turn_text",
                               return_value="画一只猫"):
            self.assertIsNone(
                confirm_gate.intercept("comfy", skill="anima_clear",
                                       prompt="cat", skip_confirm=True))
            # 不带 skip_confirm 的 agent 路径照旧拦
            self.assertIsNotNone(
                confirm_gate.intercept("comfy", skill="anima_clear",
                                       prompt="cat"))


if __name__ == "__main__":
    unittest.main()
