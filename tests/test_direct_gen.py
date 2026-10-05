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
- 引用图 + 意见 → 改图管道；意见不是修改请求（skip）→ @ 轮回反推文本、
  关键词轮闭嘴吞轮（""）
- @ 轮和关键词轮兜底 = 菜单——没引用/没图的轮要么生图要么菜单，
  绝不掉回 agent 接话（233 粉丝群 02:06 实录教训）
"""
import unittest
from unittest import mock

from app import direct_gen, image_jobs, random_tags
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
        for t in ("菜单", "！菜单", "help", "帮助", "指令",
                  ""):                      # 裸 @/裸名字（剥完啥都不剩）
            with self.subTest(t=t):
                self.assertEqual(direct_gen.decide(t, [], False),
                                 direct_gen.MENU_TEXT)

    def test_guide_keywords_return_detailed_constant(self):
        # 2026-10-05 用户拍板：/菜单 和「使用指南」= 详细版；裸「菜单」
        # 维持短菜单。全部零 LLM。
        for t in ("/菜单", "／菜单", "使用指南", "详细使用指南",
                  "使用说明", "胡桃桃：使用指南"):
            with self.subTest(t=t):
                with mock.patch.object(direct_gen.llm, "call_llm") as m_llm:
                    out = direct_gen.decide(t, [], False)
                self.assertEqual(out, direct_gen.GUIDE_TEXT)
                m_llm.assert_not_called()
        # 短菜单里要指路到详细版
        self.assertIn("使用指南", direct_gen.MENU_TEXT)

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

    def test_keyword_chatter_returns_menu_not_agent(self):
        # 233 粉丝群 02:06 实录：闲聊句命中关键词曾掉回 agent 接话
        # （「大大怪，到」）。用户拍板：关键词轮没引用/没图 → 一律菜单，
        # 绝不回聊天，零 LLM
        with mock.patch.object(direct_gen.llm, "call_llm") as m_llm:
            out = direct_gen.decide("进黑名单你都喊不出大大怪",
                                    [], False, at_me=False)
        self.assertEqual(out, direct_gen.MENU_TEXT)
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
        # 转译失败：@ 轮和关键词轮都回格式提示+菜单，谁也不掉回 agent
        with mock.patch.object(direct_gen.llm, "call_llm",
                               side_effect=RuntimeError("429")), \
             mock.patch.object(gi, "_generate_image") as m_gen:
            out_at = direct_gen.decide("画一只猫", [], False, at_me=True)
            out_kw = direct_gen.decide("画一只猫", [], False, at_me=False)
        self.assertIn(MENU_TEXT, out_at)
        self.assertIn(MENU_TEXT, out_kw)
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

    def _decide(self, text, llm_reply, quoted, last_job=None, history=None):
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
            out = direct_gen.decide(text, history or [], False, at_me=False)
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

    def test_quoted_placeholder_is_rejected_not_translated(self):
        # 2026-10-05 02:59 实录：引用机器人自己发的回执时 get_msg 拉不到，
        # 占位符被当提示词喂转译，9B 把历史里的旧 tag 抄出来，生成与引用
        # 毫无关系。现在占位符引用一律拒绝并提示重发，零 LLM。
        for quoted in ("[引用的消息无法读取]", "[图片]", "（图片）",
                       "（没有可读内容）"):
            with self.subTest(quoted=quoted):
                out, m_llm, m_gen = self._decide("sd", "x", quoted=quoted)
                self.assertIn("没能取到", out)
                m_llm.assert_not_called()
                m_gen.assert_not_called()

    def test_quoted_translate_never_sees_history(self):
        # 引用 + 渠道词的转译调用不带最近历史：历史里有旧 tag 时 9B 照抄
        #（02:59 实录，连「krea2,」前缀都是从历史拼的）。引用正文是唯一
        # 描述来源，历史直接断掉。
        old_tags = "1girl, solo, red hair, drill hair, twin drills"
        out, m_llm, m_gen = self._decide(
            "sd",
            '{"skill": "image_gen_v1", "prompt": "1girl, hat"}',
            quoted="一个女孩在花园里",
            history=[{"role": "assistant",
                      "content": "[直达生图] krea2：%s" % old_tags}])
        self.assertEqual(out, "")
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertNotIn("red hair", sent)
        self.assertNotIn("krea2：", sent)

    def test_doubao_wrapped_english_extracts_and_passes_through(self):
        # 2026-10-05 用户场景：豆包回复整段复制来引用（客套话 + 英文段），
        # 抽出英文本体直通，客套话绝不进转译。
        quoted = ("好的，那么我给你的提示词是下面的，你可以直接去复制粘贴"
                  "进行使用：\n1girl, solo, long hair, blue eyes, white "
                  "dress, standing in a garden")
        out, m_llm, m_gen = self._decide("三档", "x", quoted=quoted)
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with(
            "1girl, solo, long hair, blue eyes, white dress, "
            "standing in a garden",
            skill="hd_3_clear", _skip_confirm=True)

    def test_quoted_plus_extra_words_merges_into_translate(self):
        # 引用 + 渠道词 + 额外话（「三档 帮我加个帽子」）→ 引用正文和补充
        # 合并进转译，引用不再被丢掉。
        out, m_llm, m_gen = self._decide(
            "三档 帮我加个帽子",
            '{"skill": "hd_3_clear", "prompt": "1girl, hat"}',
            quoted="1girl in a garden")
        self.assertEqual(out, "")
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("1girl in a garden", sent)     # 引用正文进来了
        self.assertIn("帮我加个帽子", sent)           # 补充话进来了
        m_gen.assert_called_once_with("1girl, hat",
                                      skill="hd_3_clear",
                                      _skip_confirm=True)

    def test_quote_without_channel_word_never_burns_api(self):
        # 引用 + 没渠道词 + 说话（「生图」「这词什么意思」）→ 零 API 固定
        # 指路，不再烧一次转译对着「生图」两个字瞎编。
        for text in ("生图", "这个词是什么意思", "能不能给我改"):
            with self.subTest(text=text):
                out, m_llm, m_gen = self._decide(
                    text, '{"skill": "anima_clear", "prompt": "x"}',
                    quoted="1girl, solo, red hair")
                self.assertIn("没说渠道", out)
                m_llm.assert_not_called()
                m_gen.assert_not_called()


class QuoteImageIntentTest(unittest.TestCase):
    """引用图的生成/图生图意图（2026-10-05 用户拍板的边界）。"""

    def _decide(self, text, llm_reply='{"skill": "anima_clear", "prompt": "x"}',
                seen="1girl, solo, blue hair", quoted="", at_me=False,
                lookup_row=None):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value=llm_reply), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=quoted), \
             mock.patch("app.vision.describe", return_value=seen), \
             mock.patch.object(direct_gen.image_log, "lookup",
                               return_value=lookup_row), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False,
                                    data_urls=["data:image/jpeg;base64,A"],
                                    at_me=at_me)
            direct_gen._LAST_JOB.pop("group_1", None)
        return out, m_gen

    def test_gen_intent_without_channel_reverses_then_generates(self):
        # 引用图 +「帮我生成这个 / 跑一下这张图片」（没渠道）→ 用户口径：
        # 默认反推 → 重画，别反问。
        for text in ("帮我生成这个", "跑一下这张图片", "处理一下这张图"):
            with self.subTest(text=text):
                out, m_gen = self._decide(text)
                self.assertEqual(out, "")
                m_gen.assert_called_once_with("1girl, solo, blue hair",
                                              skill="anima_clear",
                                              _skip_confirm=True)

    def test_i2i_intent_pins_source_image(self):
        # 引用图 +「图生图 把头发换成银色」→ 垫图重绘（source_image=1）。
        out, m_gen = self._decide(
            "图生图 把头发换成银色",
            seen='{"skill": "anima_clear", "prompt": "1girl, silver hair"}')
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, silver hair",
                                      skill="anima_clear",
                                      _skip_confirm=True, source_image="1")

    def test_i2i_with_hd3_falls_back_to_redraw_capable(self):
        # 「三档 图生图」→ hd_3 不支持重绘，自动落回默认动漫档
        out, m_gen = self._decide(
            "三档 图生图",
            seen='{"skill": "anima_clear", "prompt": "1girl, fixed"}')
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_args.kwargs["skill"], "anima_clear")
        self.assertEqual(m_gen.call_args.kwargs["source_image"], "1")

    def test_qwen_i2i_instruction_passes_through_zero_llm(self):
        # 点名 qwen 图生图：一句改动指令直通（qwen 参考图编辑吃自然语言），
        # 连视觉调用都不用。
        out, m_gen = self._decide(
            "qwen 图生图 把外套换成红色",
            seen="不该被用到", at_me=True)
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("把外套换成红色",
                                      skill="qwen_image_v1",
                                      _skip_confirm=True, source_image="1")

    def test_channel_with_image_still_generates_from_reverse(self):
        # 原有行为不回归：引用图 +「三档」→ 反推后直接生成（不垫图）。
        out, m_gen = self._decide("三档", at_me=True)
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, solo, blue hair",
                                      skill="hd_3_clear",
                                      _skip_confirm=True)

    def test_bare_at_with_image_still_returns_reverse_text(self):
        # 原有行为不回归：引用图 + 裸 @ → 反推文本，不生成。
        out, m_gen = self._decide("", at_me=True)
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("blue hair", out)
        m_gen.assert_not_called()


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
    """改图管道：引用图 + 意见 → 按「账本有没有」分流（2026-10-05 拍板）。

    自家 HT 图 + 没点名识图 → **纯文本修正**（账本提示词当基底，走降级链，
    不花识图钱）；点名识图或别人的图 → 一次带图调用（钉死 DeepSeek 官方）。
    """

    def _decide(self, text, seen='{"skill": "anima_clear", '
                                '"prompt": "1girl, fixed"}',
                quoted="", at_me=False, last_job=None, lookup_row=None,
                describe_side_effect=None, llm_reply=None):
        with mock.patch.object(direct_gen, "llm") as m_llm_mod, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=quoted), \
             mock.patch("app.vision.describe",
                        side_effect=describe_side_effect,
                        return_value=seen) as m_describe, \
             mock.patch.object(direct_gen.image_log, "lookup",
                               return_value=lookup_row), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            if llm_reply is not None:
                m_llm_mod.call_llm.return_value = llm_reply
            if last_job:
                direct_gen._remember_job("group_1", last_job["skill"],
                                         last_job["prompt"])
            out = direct_gen.decide(text, [{"role": "user",
                                            "content": "画个女孩"}],
                                    False, data_urls=["data:image/jpeg;base64,A"],
                                    at_me=at_me)
            direct_gen._LAST_JOB.pop("group_1", None)
        return out, m_describe, m_llm_mod.call_llm, m_gen

    def test_revision_single_call_sees_image_and_opinion(self):
        # 「手改成插兜」只说了改动内容、没点名图生图 → **不垫图**（10-05
        # 用户拍板：反复垫图会越改越糊），反推修正后重画一张。
        out, m_describe, m_llm, m_gen = self._decide(
            "手改成插兜",
            '{"skill": "anima_clear", "prompt": "1girl, hands in pockets"}')
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, hands in pockets",
                                      skill="anima_clear",
                                      _skip_confirm=True)
        m_describe.assert_called_once()          # 只有一次带图调用
        self.assertNotIn("provider",
                         m_describe.call_args.kwargs)  # 跟识图配置走
        sent = m_describe.call_args.kwargs["prompt"]
        self.assertIn("手改成插兜", sent)        # 用户意见进来了
        self.assertIn("忽略此项", sent)          # 没有账本就不给原提示词
        m_llm.assert_not_called()                # 转译链路完全不参与

    def test_edit_opinion_without_verbs_regenerates_t2i(self):
        # 「手怎么多了一根」没有改图动词 → 不垫图，反推修正后重画一张
        out, m_describe, m_llm, m_gen = self._decide(
            "手怎么多了一根",
            '{"skill": "anima_clear", "prompt": "1girl, five fingers"}')
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, five fingers",
                                      skill="anima_clear",
                                      _skip_confirm=True)
        self.assertNotIn("source_image", m_gen.call_args.kwargs)
        sent = m_describe.call_args.kwargs["prompt"]
        self.assertIn("手怎么多了一根", sent)     # 用户意见进来了
        self.assertIn("忽略此项", sent)          # 没有账本就不给原提示词
        m_llm.assert_not_called()                # 转译链路完全不参与
        m_describe.assert_called_once()          # 只有一次带图调用
        self.assertNotIn("provider",
                         m_describe.call_args.kwargs)  # 跟识图配置走

    def test_own_image_text_only_revision(self):
        # 引用自家 HT 图 + 意见 → **纯文本修正**（10-05 用户拍板：账本里有
        # 当时真实提示词，看图是白花的钱）；不带历史、不带图，走降级链。
        out, m_describe, m_llm, m_gen = self._decide(
            "手改成插兜",
            llm_reply='{"prompt": "miku, hands in pockets"}',
            quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertEqual(out, "")
        m_describe.assert_not_called()           # 不看图
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("logged, miku", sent)      # 账本提示词当基底
        self.assertIn("手改成插兜", sent)        # 用户意见进来了
        self.assertNotIn("最近对话", sent)       # 历史不进场（防抄旧 tag）
        m_gen.assert_called_once_with("miku, hands in pockets",
                                      skill="hd_3_curvy",
                                      _skip_confirm=True)

    def test_own_image_skip_on_at_round_returns_logged_prompt(self):
        # 自家图 + 夸奖（skip）+ @ 轮 → 把当时的提示词回给他（等于反推还准）
        out, m_describe, m_llm, m_gen = self._decide(
            "画得真好",
            llm_reply='{"skip": true}', at_me=True,
            quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("logged, miku", out)
        m_describe.assert_not_called()
        m_gen.assert_not_called()

    def test_own_image_skip_on_keyword_round_swallows_turn(self):
        # 自家图 + 夸奖 + 关键词轮 → 闭嘴吞轮，绝不掉回 agent
        out, _, _, m_gen = self._decide(
            "画得真好",
            llm_reply='{"skip": true}',
            quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertEqual(out, "")
        m_gen.assert_not_called()

    def test_vision_ask_overrides_ledger(self):
        # 明说「识图」→ 强制看真图（画面和提示词有出入时靠这个兜底）
        out, m_describe, m_llm, m_gen = self._decide(
            "识图 帮我改一下手",
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertEqual(out, "")
        m_describe.assert_called_once()
        m_llm.assert_not_called()                # 不走文本修正
        m_gen.assert_called_once()

    def test_own_image_bare_at_returns_logged_prompt_zero_calls(self):
        # 自家图 + 裸 @ → 直接回账本提示词，识图和 LLM 都不调
        out, m_describe, m_llm, m_gen = self._decide(
            "", quoted="编号 HT-20261005-010329-595", at_me=True,
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("logged, miku", out)
        m_describe.assert_not_called()
        m_llm.assert_not_called()
        m_gen.assert_not_called()

    def test_own_image_with_channel_word_uses_ledger_zero_calls(self):
        # 自家图 +「三档」→ 账本提示词 + 新渠道直接入队，零识图零 LLM
        out, m_describe, m_llm, m_gen = self._decide(
            "三档", quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertEqual(out, "")
        m_describe.assert_not_called()
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("logged, miku", skill="hd_3_clear",
                                      _skip_confirm=True)

    def test_own_image_tier_change_replays_ledger_seed(self):
        # 换档复刻（2026-10-05 用户拍板）：引用 HT 图 +「三档」→ 账本提示词
        # 和账本种子一起入队，构图贴近原图、只换画质工作流。
        out, m_describe, m_llm, m_gen = self._decide(
            "三档", quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "anima_clear",
                        "seed": "414004422"})
        self.assertEqual(out, "")
        m_describe.assert_not_called()
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("logged, miku", skill="hd_3_clear",
                                      _skip_confirm=True, seed="414004422")

    def test_own_image_seed_not_passed_to_nai_channel(self):
        # NAI 不认 seed（传了直接报错）：引用 HT 图 +「nai」→ 不带种子入队。
        out, m_describe, m_llm, m_gen = self._decide(
            "nai", quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "anima_clear",
                        "seed": "414004422"})
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("logged, miku", skill="nai",
                                      _skip_confirm=True)
        self.assertNotIn("seed", m_gen.call_args.kwargs)

    def test_own_image_without_seed_stays_random(self):
        # 老账本记录没有 seed（空串）→ 照旧随机，不传 seed 参数。
        out, m_describe, m_llm, m_gen = self._decide(
            "三档", quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "anima_clear",
                        "seed": ""})
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("logged, miku", skill="hd_3_clear",
                                      _skip_confirm=True)
        self.assertNotIn("seed", m_gen.call_args.kwargs)

    def test_foreign_image_ignores_last_job(self):
        # 引用别人的图：上一轮任务的原提示词绝不进场（锚死事故的根因）
        out, m_describe, _, m_gen = self._decide(
            "手改成插兜",
            '{"skill": "anima_clear", "prompt": "1girl, fixed"}',
            last_job={"skill": "anima_clear", "prompt": "1girl, old"})
        self.assertEqual(out, "")
        sent = m_describe.call_args.kwargs["prompt"]
        self.assertNotIn("1girl, old", sent)
        self.assertIn("忽略此项", sent)
        m_gen.assert_called_once()

    def test_bare_at_with_image_returns_reverse_text(self):
        # 2026-10-05 用户口径：引用图 + 只 @（没别的说）→ 反推提示词返回，
        # 不生成（走 _recall_tags，跟识图配置走）
        out, m_describe, m_llm, m_gen = self._decide(
            "", seen="1girl, solo, blue hair", at_me=True)
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("blue hair", out)
        self.assertNotIn("provider", m_describe.call_args.kwargs)
        m_llm.assert_not_called()
        m_gen.assert_not_called()

    def test_channel_with_image_generates_from_reverse_zero_llm(self):
        # 引用图 + 只打档位（「三档」）→ 反推后直接生成，零 LLM
        out, m_describe, m_llm, m_gen = self._decide(
            "三档", seen="1girl, solo, blue hair", at_me=True)
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("1girl, solo, blue hair",
                                      skill="hd_3_clear", _skip_confirm=True)

    def test_generic_i2i_filler_with_channel(self):
        # 引用图 +「快档 基于图片帮我生成」→ 空话不算意见，反推后直接生成
        out, _, m_llm, m_gen = self._decide(
            "快档 基于图片帮我生成", seen="1girl, solo, blue hair", at_me=True)
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("1girl, solo, blue hair",
                                      skill="hd_fast_clear",
                                      _skip_confirm=True)

    def test_praise_on_at_round_returns_reverse_text(self):
        # @ 轮意见不是修改请求 → 一次调用里直接给出反推（不刷菜单、不二调）
        out, m_describe, _, m_gen = self._decide(
            "画得真好", seen='{"reverse": "1girl, solo, blue hair"}',
            at_me=True)
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("blue hair", out)
        m_describe.assert_called_once()
        m_gen.assert_not_called()

    def test_praise_is_swallowed_on_keyword_round(self):
        # 关键词轮引用图 + 夸奖（reverse）→ 闭嘴吞轮（""），绝不掉回 agent
        out, _, _, m_gen = self._decide(
            "比大大怪快五秒左右",
            seen='{"reverse": "1girl, solo, blue hair"}')
        self.assertEqual(out, "")
        m_gen.assert_not_called()

    def test_revision_unparseable_returns_error(self):
        out, _, _, m_gen = self._decide("手改成插兜", seen="不是 JSON")
        self.assertIn("没解析出来", out)
        m_gen.assert_not_called()

    def test_revision_call_failure_returns_error(self):
        out, m_describe, _, m_gen = self._decide(
            "手改成插兜", describe_side_effect=RuntimeError("429"))
        self.assertIn("没发出去", out)
        m_describe.assert_called_once()
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

    def test_bare_english_private_passthrough_default_channel(self):
        # 2026-10-05 用户口径：私聊裸发英文提示词（没打渠道词）→ 英文就是
        # 最终提示词，直通默认渠道零调用；群关键词轮不放行（别人贴的英文
        # 句子不该触发生成）。
        with mock.patch.object(direct_gen.llm, "call_llm") as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide("1girl, solo, silver hair, moonlight",
                                    [], False, at_me=True)
            direct_gen._LAST_JOB.pop("group_1", None)
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("1girl, solo, silver hair, moonlight",
                                      skill="anima_clear",
                                      _skip_confirm=True)

    def test_bare_english_keyword_round_not_intercepted(self):
        with mock.patch.object(direct_gen.llm, "call_llm") as m_llm, \
             mock.patch.object(gi, "_generate_image") as m_gen:
            out = direct_gen.decide("just chatting about anime stuff",
                                    [], False, at_me=False)
        self.assertEqual(out, MENU_TEXT)
        m_llm.assert_not_called()
        m_gen.assert_not_called()


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


class DescribeImageTest(unittest.TestCase):
    """看图说话：@ 轮 + 引用图 + 描述类问题 → 单次识图中文描述。

    不进改图管道（那口是奔着生成提示词去的）；只认 @ 轮；渠道词在场时不
    劫（「sd 这画的是什么」仍走改图）；图生图机制词优先级更高。
    """

    def _decide(self, text, urls=("img1",), at_me=True,
                describe_reply="红色的正方形。"):
        with mock.patch.object(direct_gen, "llm") as m_llm_mod, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch("app.vision.describe",
                        return_value=describe_reply) as m_describe, \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False, data_urls=list(urls),
                                    at_me=at_me)
        return out, m_describe, m_gen, m_llm_mod

    def test_describe_question_answers_in_chinese(self):
        out, m_describe, m_gen, m_llm = self._decide("这画的是什么？")
        self.assertEqual(out, "红色的正方形。")
        m_describe.assert_called_once()
        self.assertEqual(m_describe.call_args.kwargs.get("prompt"),
                         direct_gen._DESCRIBE_PROMPT)
        m_gen.assert_not_called()
        m_llm.call_llm.assert_not_called()      # 纯识图，文本链路不参与

    def test_describe_not_hijack_channel_revise(self):
        # 渠道词在场 → 改图管道优先，识图 prompt 不是看图说话那套
        out, m_describe, _m_gen, _m_llm = self._decide("sd 这画的是什么")
        self.assertNotEqual(m_describe.call_args.kwargs.get("prompt"),
                            direct_gen._DESCRIBE_PROMPT)
        self.assertIn("改图请求没解析出来", out)

    def test_keyword_round_no_describe(self):
        # 关键词轮不启用看图说话（群聊保持安静口径）
        _out, m_describe, _m_gen, _m_llm = self._decide("这画的是什么",
                                                        at_me=False)
        self.assertNotEqual(m_describe.call_args.kwargs.get("prompt"),
                            direct_gen._DESCRIBE_PROMPT)


class MultiImageReverseTest(unittest.TestCase):
    """多图反推：渠道+档位分支放开到 3 张合并；生成路径不带中文序号。"""

    def _decide(self, text, urls, describe_side):
        with mock.patch.object(direct_gen, "llm") as _m_llm_mod, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch("app.vision.describe",
                        side_effect=describe_side), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False, data_urls=list(urls),
                                    at_me=True)
        return out, m_gen

    def test_tier_branch_merges_three_images_unnumbered(self):
        out, m_gen = self._decide(
            "三档", ["a", "b", "c"],
            ["1girl", "2girls", "3girls"])
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_count, 1)
        self.assertEqual(m_gen.call_args.args[0],
                         "1girl, 2girls, 3girls")
        self.assertNotIn("（第", m_gen.call_args.args[0])

    def test_bare_at_reverse_keeps_numbering(self):
        # 裸 @ + 多图（回给人看的反推文本）保留「（第 N 张）」序号
        out, m_gen = self._decide(
            "", ["a", "b"], ["tags one", "tags two"])
        self.assertIn("（第 2 张）tags two", out)
        m_gen.assert_not_called()


class PrivateQATest(unittest.TestCase):
    """私聊单轮问答：问号结尾 → 单次调用直答；群聊铁律不破。"""

    def _decide(self, text, history=None, key="private_9", at_me=True,
                llm_reply="NovelAI 是一个 AI 绘画服务。"):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value=llm_reply) as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value=key), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, list(history or []), False,
                                    at_me=at_me)
            self.addCleanup(direct_gen.pop_qa, key)
        return out, m_llm, m_gen

    def test_private_question_gets_single_llm_answer(self):
        out, m_llm, m_gen = self._decide("你知道NovelAI是什么吗？")
        self.assertEqual(out, "NovelAI 是一个 AI 绘画服务。")
        m_llm.assert_called_once()              # 单次调用，无循环
        m_gen.assert_not_called()
        msgs = m_llm.call_args.args[0]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertEqual(msgs[-1]["content"], "你知道NovelAI是什么吗？")
        # 本轮问答进了落史暂存，qq_bot 送出后 pop 落史
        self.assertEqual(direct_gen.pop_qa("private_9"),
                         ("你知道NovelAI是什么吗？",
                          "NovelAI 是一个 AI 绘画服务。"))

    def test_english_question_not_treated_as_prompt(self):
        # 「who are you?」是英文问句，不该被裸英文直通拿去生图
        out, m_llm, m_gen = self._decide("who are you?")
        self.assertEqual(out, "NovelAI 是一个 AI 绘画服务。")
        m_gen.assert_not_called()
        m_llm.assert_called_once()

    def test_group_question_still_menu(self):
        # 群聊铁律：问句结尾照样菜单，绝不聊天。**@ 轮也要测**——只测
        # 关键词轮（at_me=False）的话，at_me 那层短路会把 session 判定
        # 的破坏掩护过去（反证实测）。
        out, m_llm, m_gen = self._decide("你知道NovelAI是什么吗？",
                                         key="group_1", at_me=False)
        self.assertEqual(out, MENU_TEXT)
        m_llm.assert_not_called()
        m_gen.assert_not_called()
        self.assertIsNone(direct_gen.pop_qa("group_1"))

    def test_group_at_round_question_still_not_chat(self):
        # 群聊 @ 轮 + 问号 → 也不进问答，走转译兜底回菜单
        out, m_llm, m_gen = self._decide(
            "你知道NovelAI是什么吗？", key="group_1", at_me=True,
            llm_reply='{"skill": "", "prompt": ""}')
        self.assertIn(MENU_TEXT, out)
        m_gen.assert_not_called()

    def test_drawing_intent_not_hijacked(self):
        # 「画一只猫？」有画图动词 → 走转译生图，不进问答
        out, m_llm, m_gen = self._decide(
            "画一只猫？",
            llm_reply='{"skill": "anima_clear", "prompt": "cat"}')
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("cat", skill="anima_clear",
                                      _skip_confirm=True)

    def test_history_window_capped_and_system_head_skipped(self):
        hist = [{"role": "system", "content": "head"}]
        for i in range(8):
            hist.append({"role": "user", "content": "q%d" % i})
            hist.append({"role": "assistant", "content": "a%d" % i})
        out, m_llm, _m_gen = self._decide("还在吗？", history=hist)
        self.assertEqual(out, "NovelAI 是一个 AI 绘画服务。")
        msgs = m_llm.call_args.args[0]
        # system(问答人设) + 最近 6 条 + 本轮 = 8
        self.assertEqual(len(msgs), 8)
        self.assertEqual(msgs[1]["content"], "q5")   # 截到最后 6 条：q5 起
        self.assertNotIn("head", [m["content"] for m in msgs])

    def test_llm_failure_falls_back_to_menu(self):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               side_effect=RuntimeError("down")), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="private_9"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""):
            out = direct_gen.decide("在吗？", [], False, at_me=True)
            self.addCleanup(direct_gen.pop_qa, "private_9")
        self.assertEqual(out, MENU_TEXT)


class RandomCommandTest(unittest.TestCase):
    """随机口令（2026-10-05）：/随机萝莉 /随机兽耳 /随机女仆 /今日老婆。

    策展词池直拼提示词 → 零 LLM，默认档入队；关键词轮也认（@ 不 @ 都行），
    主动接话轮绝不触发。
    """

    def _decide(self, text, voluntary=False, at_me=True):
        with mock.patch.object(direct_gen, "llm") as m_llm_mod, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], voluntary, at_me=at_me)
            self.addCleanup(direct_gen._LAST_JOB.pop, "group_1", None)
        return out, m_gen, m_llm_mod

    def test_random_loli_enqueues_zero_llm(self):
        out, m_gen, m_llm = self._decide("/随机萝莉")
        self.assertEqual(out, "")
        m_llm.call_llm.assert_not_called()
        m_gen.assert_called_once()
        self.assertIn("loli", m_gen.call_args.args[0])
        self.assertEqual(m_gen.call_args.kwargs["skill"], "anima_clear")

    def test_random_kemono_without_slash_in_keyword_round(self):
        # 关键词轮（不 @）也认纯口令
        out, m_gen, m_llm = self._decide("随机兽耳", at_me=False)
        self.assertEqual(out, "")
        m_llm.call_llm.assert_not_called()
        self.assertIn("animal_ears", m_gen.call_args.args[0])

    def test_random_maid(self):
        out, m_gen, _m_llm = self._decide("随机女仆")
        self.assertEqual(out, "")
        self.assertIn("maid", m_gen.call_args.args[0])

    def test_waifu_uses_pool_character(self):
        out, m_gen, m_llm = self._decide("/今日老婆")
        self.assertEqual(out, "")
        m_llm.call_llm.assert_not_called()
        first_tag = m_gen.call_args.args[0].split(",")[0].strip()
        self.assertIn(first_tag,
                      {e["tag"] for e in random_tags.DEFAULT_WAIFU_POOL})

    def test_voluntary_round_never_triggers(self):
        # 主动接话轮（没人喊它）绝不出图
        out, m_gen, _m_llm = self._decide("随机萝莉", voluntary=True)
        self.assertIsNone(out)
        m_gen.assert_not_called()

    def test_command_with_extra_text_not_matched(self):
        # 口令后跟别的话 → 不是口令，走正常管道（画图动词 → 转译）
        with mock.patch.object(direct_gen, "llm") as m_llm_mod, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            m_llm_mod.call_llm.return_value = (
                '{"skill": "anima_clear", "prompt": "random girl"}')
            out = direct_gen.decide("随机萝莉 来一张", [], False, at_me=True)
            self.addCleanup(direct_gen._LAST_JOB.pop, "group_1", None)
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_args.args[0], "random girl")

    def test_more_channels_text(self):
        out, m_gen, _m_llm = self._decide("/更多渠道")
        self.assertEqual(out, direct_gen.MORE_CHAN_TEXT)
        m_gen.assert_not_called()

    # ── 被拦静默重抽（2026-10-05）：图是机器人推的服务，不回「未过审」──

    def test_random_commands_pass_resample_fn(self):
        out, m_gen, _m_llm = self._decide("/随机萝莉")
        fn = m_gen.call_args.kwargs.get("resample_fn")
        self.assertTrue(callable(fn))
        self.assertIn("loli", fn())          # 重抽还是同主题
        self.assertIn("solo", fn())

    def test_waifu_resample_redraws_from_pool(self):
        out, m_gen, _m_llm = self._decide("/今日老婆")
        fn = m_gen.call_args.kwargs.get("resample_fn")
        pool = {e["tag"] for e in random_tags.DEFAULT_WAIFU_POOL}
        self.assertIn(m_gen.call_args.args[0].split(",")[0].strip(), pool)
        self.assertIn(fn().split(",")[0].strip(), pool)   # 重抽换角色、仍出自池

    def test_user_requested_prompt_has_no_resample(self):
        # 用户点的单被拦 → 照旧回「未过审」，绝不静默换图
        with mock.patch.object(direct_gen, "llm") as m_llm_mod, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            m_llm_mod.call_llm.return_value = (
                '{"skill": "anima_clear", "prompt": "a cat"}')
            out = direct_gen.decide("画一只猫", [], False, at_me=True)
            self.addCleanup(direct_gen._LAST_JOB.pop, "group_1", None)
        self.assertEqual(out, "")
        self.assertIsNone(m_gen.call_args.kwargs.get("resample_fn"))


if __name__ == "__main__":
    unittest.main()
