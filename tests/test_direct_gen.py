#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直达生图管道（app/direct_gen.py）测试。

核心契约（2026-10-05 渠道收归代码版 + 同日「AI 必须参与决策」版）：
- /菜单、裸 @、裸名字 → 菜单常量，零 LLM
- **提示词一律由 AI 产出**：代码里没有任何「英文提示词原样入队」「引用抽英文
  段直通」「成品串原样透传」的零转译旁路（用户原话：「ai 必须参与决策，绝对
  不能绕过 ai」）。
- 代码只做关键词匹配 → 渠道词打头（档位/画风/固定渠道词）→ **代码正则**定
  渠道（档位最核心：画风打错/没打 → 该档默认 clear），LLM 写提示词；正文
  一个字不碰（全文乱搜画风词会把 `1girl, soft lighting` 的 soft 删掉）。
- 带 NAI 权号 `::` 的**成品串** → 也过一次 AI，权号/画师串规则写在唯一模板里；
  渠道「点名词优先 → AI 判 → 代码只校验合法性」，两边都判不出就回问，
  **绝不静默落默认档**。
- **唯一模板 `_MASTER_TEMPLATE`**（2026-10-05 23:xx 用户拍板）：人设 + 两个工具
  （generate_image / recall_image）+ 渠道清单 + 最近对话 + 用户原话。四条按渠道
  分家的模板已全删。`_translate` 返回三态：生图 / `{reply}` 聊天 / None。
- 转译轮的渠道优先级：代码点名的 > 模型给的（开头说了 nai 就 nai）
- 裸 @ + 描述 / 画图动词 / 私聊裸英文 → 一次转译（LLM 判渠道 + 写提示词）
- 引用正文 + 只打渠道词 → 引用内容当描述，锁定渠道交给 AI
- 引用回执 +「再来一张」→ 同提示词换种子重跑，零转译
- 引用图 + 意见 → 改图管道（**一律真识图**）；明说机制词时必出图，没点名
  机制词且模型判「不是修改请求」→ @ 轮回反推文本、关键词轮闭嘴吞轮（""）
- @ 轮和关键词轮兜底 = 菜单——没引用/没图的轮要么生图要么菜单，
  绝不掉回 agent 接话（233 粉丝群 02:06 实录教训）
"""
import json
import unittest
from unittest import mock

from app import direct_gen, image_jobs, qq_bot, random_tags
from app.direct_gen import MENU_TEXT
from app.tools.normal import generate_image as gi

RECEIPT = image_jobs.RECEIPT_SENT_MARK + "回执"

# 前置搜索（`_translate` 里的 search_agent）默认关掉。
#
# 为什么必须显式关：这些用例测的是**转译本身**，而搜索会给每次转译多打一次
# LLM。更麻烦的是 search_agent 用的是同一个 `app.llm` 模块对象——用例里
# `mock.patch.object(direct_gen.llm, "call_llm")` 会**连带把搜索那一路也
# mock 掉**，返回 MagicMock 而不是字符串，parse_tool_calls 拿到它行为未定义。
# 与其让每个用例各自处理，不如模块级统一关掉；要测搜索这一层的看
# `SearchHookTest`（它自己开）。
_search_off = None


def setUpModule():
    global _search_off
    _search_off = mock.patch.object(direct_gen, "SEARCH_ENABLED", False)
    _search_off.start()


def tearDownModule():
    if _search_off:
        _search_off.stop()


def _pc(text):
    """`_parse_channel` 的返回值 (skill, desc)。

    2026-10-07 起它回到两元组（原先那个 `alt` 第二渠道词上报已删——用户拍板
    「严格从左到右取第一个命中的」，不再上报）。保留这个小包装只为老用例
    少改字。
    """
    return direct_gen._parse_channel(text)


class ChannelParseTest(unittest.TestCase):
    """渠道解析收归代码：判据必须确定性，用群聊实录出题。"""

    def test_tier_and_style(self):
        for text, skill, desc in (
                ("三档 clear 初音未来", "hd_3_clear", "初音未来"),
                ("三档 soft 初音未来", "hd_3_soft", "初音未来"),
                ("二档 gloss 女骑士", "hd_2_gloss", "女骑士"),
                ("快档 一只柴犬在草地上", "hd_fast_clear", "一只柴犬在草地上"),
                ("默认初音未来", "silver", "初音未来"),   # 无分隔符（2026-10-06 默认渠道 = silver）
                ("默认，纳西妲", "silver", "纳西妲"),
                ("默认 gloss 一个女孩", "anima_gloss", "一个女孩"),
                ("gloss 一个女孩", "anima_gloss", "一个女孩"),  # 只打画风
                ("三档 猫", "hd_3_clear", "猫"),               # 画风没打
                ("一档curvy 初音未来", "hd_fast_curvy", "初音未来"),  # 连写
                ("nai 1girl, masterpiece", "nai", "1girl, masterpiece")):
            with self.subTest(text=text):
                got_skill, got_desc = _pc(text)
                self.assertEqual((got_skill, got_desc), (skill, desc))

    def test_fixed_channel_words(self):
        # 用户点名的固定渠道也要代码直判，不劳 LLM（2026-10-05）
        for text, skill, desc in (
                ("sd 一只猫", "image_gen_v1", "一只猫"),
                ("krea2 一个女孩", "krea2", "一个女孩"),
                ("qwen 写实街拍", "qwen_image_v1", "写实街拍"),
                ("nffa 插画少女", "nffa", "插画少女"),
                ("cunny 一个女孩", "cunny", "一个女孩"),   # 2026-10-05 新渠道
                ("miao 一个女孩", "miao", "一个女孩"),     # 2026-10-05 新渠道
                ("跑个cunny 白发兽耳", "cunny", "跑个  白发兽耳"),  # 动词残渣同 nai
                ("这个猪 跑 nai", "nai", "这个猪"),   # 渠道词不限位置+动词残渣
                ("nai 伊藤润二画风", "nai", "伊藤润二画风")):
            with self.subTest(text=text):
                self.assertEqual(_pc(text),
                                 (skill, desc))

    def test_fixed_word_inside_english_prompt_is_ignored(self):
        # 档位在场时固定渠道词不参与（英文提示词里撞词不误判）
        self.assertEqual(
            _pc("三档 1girl, solo, sd style"),
            ("hd_3_clear", "1girl, solo, sd style"))

    def test_typo_style_falls_back(self):
        # 画风词打错：贴得回来（glss→gloss）就修正；贴不回来按档位默认 clear，
        # 原词留在描述里不丢。
        self.assertEqual(_pc("三档,glss,初音未来"),
                         ("hd_3_gloss", "初音未来"))
        skill, desc = _pc("三档,miku,初音未来")
        self.assertEqual(skill, "hd_3_clear")
        self.assertIn("miku", desc)

    def test_head_channel_word_beats_style_word(self):
        # 2026-10-05 私聊 2509355624 实录：「nai，真人 Cos 阿米娅…柔和…」被
        # 「柔和」抢走画风 → anima_soft。用户原话「我写了 nai 了！前缀已经是
        # nai 了！」：**开头的渠道词最高优先**，画风词只是画面内容留在描述里。
        for text, skill, desc in (
                ("nai，真人 Cos 阿米娅，蓝白制服，柔和光线",
                 "nai", "真人 Cos 阿米娅，蓝白制服，柔和光线"),
                ("nai 凯尔西 柔和", "nai", "凯尔西 柔和"),
                ("qwen 柔和光线的少女", "qwen_image_v1", "柔和光线的少女"),
                ("nffa curvy 少女", "nffa", "curvy 少女")):
            with self.subTest(text=text):
                self.assertEqual(_pc(text),
                                 (skill, desc))
        # 不在开头的渠道词照旧让位档位（英文 tag 撞词不误判）
        self.assertEqual(_pc("三档 1girl, solo, sd style"),
                         ("hd_3_clear", "1girl, solo, sd style"))

    def test_style_words_inside_the_body_are_not_channel_words(self):
        # 2026-10-05 实录（用户原话「英文提示词直接绕过 ai 结果导致一大堆的
        # 问题」）：画风词以前是 `_STYLE_RE.search(text)` **全文乱搜**，于是
        # `1girl, soft lighting` 的 soft 被当画风词——渠道抢成 anima_soft，
        # 那个词还被从正文里删掉（→ `1girl,   lighting`）。现在只在开头
        # 命令区认画风词，正文原样返回。
        for text in ("1girl, soft lighting, blue hair, best quality",
                     "masterpiece, gloss finish, portrait",
                     "1girl, curvy, swimsuit",
                     "nurse, clear eyes, 1girl",
                     "soft lighting, 1girl"):
            with self.subTest(text=text):
                self.assertEqual(_pc(text), (None, text))

    def test_danbooru_tag_with_underscores_is_not_a_channel_word(self):
        # `anime_nffa_1` 里的 nffa 前后都挨着下划线 → 不是点名词，不能从
        # 中间把提示词剪断。
        text = "solo, anime_nffa_1"
        self.assertEqual(_pc(text), (None, text))

    def test_reported_private_log_nai_case_locks_nai(self):
        # 2026-10-05 私聊 2831674699 实录（用户报「明明都已经说了 nai 和中文
        # 需求了，还是调用 anima」）：日志里第 28、34 行两条都是
        # `nai，真人 Cos 阿米娅…柔和室内光…`，被「柔和」抢成 anima_soft。
        # 现在开头的 nai 锁死渠道，正文整段原样交给 AI。
        text = ("nai，真人 Cos 阿米娅，角色服装以蓝白色为主，保留阿米娅的标志性"
                "配色与耳部装饰，人物站在书房落地镜前自拍，柔和室内光，低饱和"
                "色调，自然肤色，整体干净、文艺、安静。")
        skill, desc = _pc(text)
        self.assertEqual(skill, "nai")
        self.assertTrue(desc.startswith("真人 Cos 阿米娅"))
        self.assertIn("柔和室内光", desc)

    def test_positional_priority_first_keyword_wins(self):
        # 2026-10-05 用户拍板原话：「从开头开始匹配，第一个匹配到的是谁…
        # 谁靠前，谁的优先级最高」。固定渠道词和档位词**不按类型排序**，
        # 只看位置——「nai 只有 nai，没有档位；档位/画风是 anima 那一家的事」。
        for text, skill in (
                # 日志 #24/#26 实录：NAI 和 三档 同时出现 → NAI 靠前 → nai
                ("NAI，COSER，真人，亚洲少女，凯尔希，1girl，三档", "nai"),
                ("三档 nai 猫", "hd_3_clear"),      # 三档在位置 0
                ("猫 三档 nai", "hd_3_clear"),      # 三档@2 比 nai@5 靠前
                ("猫 nai 三档", "nai"),             # nai@2 比 三档@6 靠前
                ("sd 三档 猫", "image_gen_v1"),
                ("三档 sd 猫", "hd_3_clear"),
                # 档位词落在句子中间也要认（旧代码认，别退化）
                ("画个女孩 三档", "hd_3_clear"),
                ("画个女孩 三档 gloss", "hd_3_gloss"),
                # 档位和画风同族，谁前谁后都组合（「二档 gloss」的老用法反过来）
                ("gloss 三档 猫", "hd_3_gloss")):
            with self.subTest(text=text):
                self.assertEqual(_pc(text)[0], skill)

    def test_no_channel_word_returns_none(self):
        text = "初音未来，全身照，anime，正身平齐视角"
        self.assertEqual(_pc(text), (None, text))

    def test_own_name_expansion_is_stripped(self):
        # 群实录 2026-10-05 01:07：文字 @ 的昵称带括号扩展，顶着名字渠道词
        # 永远匹配不上。
        stripped = direct_gen._strip_own_names(
            "@大大怪（生图机器人，贼拉快，种类多） 三档 soft 初音未来")
        self.assertEqual(_pc(stripped),
                         ("hd_3_soft", "初音未来"))


class LeftmostChannelTest(unittest.TestCase):
    """两个渠道词并存时，**严格取从左到右第一个命中的**（2026-10-07 用户拍板）。

    用户原话：「他打 Six 三档，那就走 Six；如果是三档 Six，那就走三档——就看
    哪一个排在第一」。原先那套「上报第二个词让 AI 判」的 alt 机制已删。
    """

    def test_leftmost_channel_word_wins(self):
        for text, skill in (
                ("silver 三档", "hd_3_clear"),
                ("三档 silver", "hd_3_clear"),
                ("jank 二档", "hd_2_clear"),
                ("二档 jank", "hd_2_clear"),
                ("silver 快档", "hd_fast_clear"),
                ("silver 三档 gloss", "hd_3_gloss"),
                ("silver 三档 女骑士", "hd_3_clear"),
                # 固定渠道词排在档位词前面 → 固定词赢
                ("nai 三档", "nai"),
                # 档位词排在固定词前面 → 档位词赢
                ("三档 qwen", "hd_3_clear"),
                ("画个女孩 三档 qwen", "hd_3_clear")):
            with self.subTest(text=text):
                self.assertEqual(direct_gen._parse_channel(text)[0], skill)

    def test_no_alt_pollution_in_desc(self):
        # 认不出的自定义渠道词（silver / jank）留在正文里，等 AI 判——代码
        # 不上报、不裁决。
        self.assertEqual(direct_gen._parse_channel("silver 三档"),
                         ("hd_3_clear", "silver"))
        self.assertEqual(direct_gen._parse_channel("silver 三档 女骑士"),
                         ("hd_3_clear", "silver  女骑士"))

    def test_chan_hint_stays_a_command_when_unambiguous(self):
        """只有一个渠道词时维持老口径（「就用它」），别把 AI 搞犹豫。"""
        captured = {}

        def fake_ask(content):
            captured["content"] = content
            return {"reply": "好的"}

        with mock.patch.object(direct_gen, "_ask", side_effect=fake_ask):
            direct_gen._translate("三档 女骑士", [], skill="hd_3_clear")
        self.assertIn("就用它", captured["content"])


class MenuAndGateTest(unittest.TestCase):
    def test_menu_keywords_return_constant(self):
        # 2026-10-05 23:xx 用户拍板：**只有用户明确要菜单**才回常量。
        for t in ("菜单", "！菜单", "help", "帮助", "指令"):
            with self.subTest(t=t):
                with mock.patch.object(direct_gen.llm, "call_llm") as m_llm:
                    out = direct_gen.decide(t, [], False)
                self.assertEqual(out, direct_gen.MENU_TEXT)
                m_llm.assert_not_called()

    def test_bare_at_is_not_taken_over(self):
        # 裸 @ / 裸名字（剥完啥都不剩）→ 没内容可判，不接管（交回上层），
        # **不再回菜单**（2026-10-05 用户拍板：删掉菜单触发）。
        with mock.patch.object(direct_gen.llm, "call_llm") as m_llm:
            self.assertIsNone(direct_gen.decide("", [], False))
        m_llm.assert_not_called()

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

    def test_menu_mentions_the_4x_channel(self):
        """短菜单给 silver-hd 一行曝光（2026-10-07 用户拍板）——短菜单本身
        不列渠道，但「4x 超清」这条得让用户看得见。"""
        self.assertIn("silver-hd", direct_gen.MENU_TEXT)

    def test_attribution_prefix_is_stripped_before_menu_match(self):
        self.assertEqual(
            direct_gen.decide("胡桃桃：菜单", [], False),
            direct_gen.MENU_TEXT)

    def test_voluntary_turns_never_taken_over(self):
        self.assertIsNone(direct_gen.decide("画一只猫", [], True))

    def test_at_chatter_goes_to_ai_and_replies(self):
        # @ 轮闲聊 → 过一次 AI；AI 判「是聊天」→ 把 reply 发出去（不回菜单）
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"skill": "", "prompt": "", '
                                            '"reply": "哈哈，今天确实不错"}'
                               ) as m_llm:
            out = direct_gen.decide("今天天气不错", [], False, at_me=True)
        self.assertEqual(out, "哈哈，今天确实不错")
        self.assertEqual(m_llm.call_count, 1)

    def test_keyword_chatter_now_goes_to_ai(self):
        # 2026-10-05 23:xx 用户拍板：命中触发词不再回菜单——一律交给 AI 判
        # 意图（是画图就画，是聊天就回话）。关键词轮（at_me=False）也一样。
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"skill": "", "prompt": "", '
                                            '"reply": "我在"}') as m_llm:
            out = direct_gen.decide("进黑名单你都喊不出大大怪",
                                    [], False, at_me=False)
        self.assertEqual(out, "我在")
        self.assertEqual(m_llm.call_count, 1)


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
        self.assertIn("用户开头点名了渠道：**hd_3_clear**", sent)
        self.assertIn("初音未来", sent)

    def test_typo_style_resolves_in_full_pipeline(self):
        out, _, m_gen = self._decide(
            '{"skill": "hd_3_gloss", "prompt": "miku"}',
            text="三档,glss,初音未来")
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_args.kwargs["skill"], "hd_3_gloss")

    def test_head_channel_word_survives_translation(self):
        # 全链路：开头点名 nai + 描述里有画风词 → 代码锁 nai，模型回 anima_soft
        # 也不许顶掉（2026-10-05 私聊实录的那一条）。
        out, _m, m_gen = self._decide(
            '{"skill": "anima_soft", "prompt": "1girl, cosplay, soft light"}',
            text="nai，真人 Cos 阿米娅，柔和光线")
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_args.kwargs["skill"], "nai")

    def test_non_image_verdict_not_taken_over(self):
        # 模型既没判出图、也没给话 → 不接管（交回上层），**不回菜单**
        # （2026-10-05 用户拍板：删掉菜单触发）。
        out, m_llm, m_gen = self._decide('{"skill": "", "prompt": ""}')
        self.assertIsNone(out)
        m_gen.assert_not_called()

    def test_broken_json_not_taken_over(self):
        for bad in ("我不是 JSON", '{"skill": "anima_clear"',
                    '前置废话 {"skill": ok}'):
            with self.subTest(bad=bad):
                out, m_llm, m_gen = self._decide(bad)
                self.assertIsNone(out)
                m_gen.assert_not_called()

    def test_llm_failure_not_taken_over(self):
        # 转译调用失败 → 不接管（交回上层），不回菜单。
        with mock.patch.object(direct_gen.llm, "call_llm",
                               side_effect=RuntimeError("429")), \
             mock.patch.object(gi, "_generate_image") as m_gen:
            out_at = direct_gen.decide("画一只猫", [], False, at_me=True)
            out_kw = direct_gen.decide("画一只猫", [], False, at_me=False)
        self.assertIsNone(out_at)
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

    def test_quoted_english_prompt_goes_through_ai(self):
        # 2026-10-05 用户拍板（「ai 必须参与决策，绝对不能绕过 ai」）：引用
        # 正文也不再抽英文段直通，锁定渠道过一次 AI。
        out, m_llm, m_gen = self._decide(
            "快档 gloss",
            '{"skill": "hd_fast_gloss", "prompt": "1girl, solo, white dress"}',
            quoted="1girl, solo, white dress, standing in a garden")
        self.assertEqual(out, "")
        self.assertEqual(m_llm.call_count, 1)
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("用户开头点名了渠道：**hd_fast_gloss**", sent)
        self.assertIn("1girl, solo, white dress, standing in a garden", sent)
        m_gen.assert_called_once_with("1girl, solo, white dress",
                                      skill="hd_fast_gloss",
                                      _skip_confirm=True)

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
        self.assertIn("用户开头点名了渠道：**hd_3_clear**", sent)

    def test_quoted_reverse_reply_goes_through_ai(self):
        # 引用机器人的反推回复 + 渠道词 → 正文照样交给 AI（不再剥头直用 tag）
        out, m_llm, m_gen = self._decide(
            "三档",
            '{"skill": "hd_3_clear", "prompt": "1girl, twintails, aqua hair"}',
            quoted=direct_gen._REVERSE_HEADER + "\n1girl, twintails, aqua hair")
        self.assertEqual(out, "")
        self.assertEqual(m_llm.call_count, 1)
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

    def test_doubao_wrapped_english_goes_through_ai(self):
        # 豆包包装（客套话 + 英文段）整段引用 → 原样喂 AI，由它自己挑出提示词
        # （以前是代码正则抽英文段直通，属于「代码替 AI 做解析」）。
        quoted = ("好的，那么我给你的提示词是下面的，你可以直接去复制粘贴"
                  "进行使用：\n1girl, solo, long hair, blue eyes, white "
                  "dress, standing in a garden")
        out, m_llm, m_gen = self._decide(
            "三档",
            '{"skill": "hd_3_clear", "prompt": "1girl, solo, long hair"}',
            quoted=quoted)
        self.assertEqual(out, "")
        self.assertEqual(m_llm.call_count, 1)
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("1girl, solo, long hair, blue eyes, white dress", sent)
        m_gen.assert_called_once_with("1girl, solo, long hair",
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

    def test_quote_without_channel_word_now_goes_to_ai(self):
        # 2026-10-05 23:xx 用户拍板：删掉「引用 + 没渠道词 → 零 API 固定
        # 指路」那条硬编码兜底——现在引用正文和用户原话合并成一条描述喂
        # `_translate`，AI 自己决定是照着画还是答话。
        for text in ("生图", "这个词是什么意思", "能不能给我改"):
            with self.subTest(text=text):
                out, m_llm, m_gen = self._decide(
                    text, '{"skill": "anima_clear", "prompt": "x"}',
                    quoted="1girl, solo, red hair")
                self.assertEqual(out, "")
                self.assertEqual(m_llm.call_count, 1)
                sent = m_llm.call_args[0][0][0]["content"]
                self.assertIn("1girl, solo, red hair", sent)   # 引用正文进来了
                m_gen.assert_called_once()


class QuoteImageIntentTest(unittest.TestCase):
    """引用图的生成/图生图意图（2026-10-05 拍板 + 2026-10-07 全交 AI）。

    2026-10-07 用户拍板：发图 + 指令**默认走 AI**，代码不再认死话术
    （`_GENERIC_I2I_RE` / `_RUN_THIS_RE` / `_IMG_GEN_INTENT_RE` 全删）。所以
    「帮我生成这个 / 跑这张」这类**当成普通意见交给 AI**，由模型判渠道+垫图。
    """

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

    def test_gen_intent_goes_through_ai(self):
        # 2026-10-07：这些「帮我生成这个」的原话不再由代码认死话术，整句交给
        # AI；没有账本 → 识图一次，模型出提示词入队。
        for text in ("帮我生成这个", "跑一下这张图片", "处理一下这张图"):
            with self.subTest(text=text):
                out, m_gen = self._decide(
                    text, seen='{"skill": "silver", "prompt": "1girl, solo, '
                               'blue hair"}')
                self.assertEqual(out, "")
                m_gen.assert_called_once_with("1girl, solo, blue hair",
                                              skill="silver",
                                              _skip_confirm=True)

    def test_the_model_decides_whether_to_pad(self):
        """垫不垫图 = 那一次调用里模型输出的 `source_image`（2026-10-06 拆闸）。

        以前是代码扫原话：命中「图生图」就**强行** `source_image=1`。现在原话
        照旧送给模型，它说垫才垫——它说「改提示词重画」就重画，一个字节都不垫。
        """
        out, m_gen = self._decide(
            "图生图 把头发换成银色",
            seen='{"skill": "qwen_image_v1", "prompt": "1girl, silver hair",'
                 ' "source_image": 1}')
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, silver hair",
                                      skill="qwen_image_v1",
                                      _skip_confirm=True, source_image="1")

    def test_no_source_image_from_the_model_means_no_pad(self):
        """同一条原话，模型没填 `source_image` → 就是文生图，代码不补。"""
        out, m_gen = self._decide(
            "图生图 把头发换成银色",
            seen='{"skill": "silver", "prompt": "1girl, silver hair"}')
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, silver hair", skill="silver",
                                      _skip_confirm=True)

    def test_i2i_with_hd3_is_not_silently_downgraded(self):
        """「三档 图生图」不再被代码悄悄降回动漫档。

        旧的 `_redraw_capable` 会把渠道换成 `anima_clear`——出图风味全变，对方
        要的是 hd_3，拿到的却是 728×1024。现在渠道照模型/渠道词说的走，
        **能不能垫图由 generate_image 报错说话**（那里只剩能力判断）。
        """
        out, m_gen = self._decide(
            "三档 图生图 把头发修一下",
            seen='{"skill": "hd_3_clear", "prompt": "1girl, fixed",'
                 ' "source_image": 1}')
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_args.kwargs["skill"], "hd_3_clear")
        self.assertEqual(m_gen.call_args.kwargs["source_image"], "1")

    def test_qwen_i2i_locks_the_channel_but_the_model_pads(self):
        """点名 qwen 图生图：渠道词由代码认出并锁定，垫图与否仍听模型。

        2026-10-06 拆的是「扫原话认机制词 → 零调用直接入队垫图」那条路由；
        现在这一轮照样进改图管道（一次带图调用），模型 JSON 里给
        `source_image: 1` 才真的垫。
        """
        out, m_gen = self._decide(
            "qwen 图生图 把外套换成红色",
            seen='{"prompt": "change her coat to red, keep everything else'
                 ' exactly the same", "source_image": 1}', at_me=True)
        self.assertEqual(out, "")
        m_gen.assert_called_once_with(
            "change her coat to red, keep everything else exactly the same",
            skill="qwen_image_v1", _skip_confirm=True, source_image="1")

    def test_channel_with_image_still_generates_from_reverse(self):
        # 原有行为不回归：引用图 +「三档」→ 反推后直接生成（不垫图）。
        out, m_gen = self._decide("三档", at_me=True)
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, solo, blue hair",
                                      skill="hd_3_clear",
                                      _skip_confirm=True)

    def test_run_this_phrase_with_channel_goes_to_ai(self):
        # 2026-10-07：「跑这张 / 跑这个」这类空话不再由代码认死（`_RUN_THIS_RE`
        # 已删），整句交给 AI 判。没有账本 → 识图一次。
        for t in ("cunny跑这张", "cunny 跑这个", "cunny 跑一下"):
            with self.subTest(t=t):
                out, m_gen = self._decide(
                    t, seen='{"skill": "cunny", "prompt": "1girl, solo, '
                            'blue hair"}')
                self.assertEqual(out, "")
                m_gen.assert_called_once_with("1girl, solo, blue hair",
                                              skill="cunny", _skip_confirm=True)

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
    """改图管道：引用图 + 意见 → 一次调用（2026-10-07 起分两条）。

    引用带图改图**自家图走纯文本**（账本优先、不预先识图——2026-10-07 用户
    拍板：「没必要每一次引用我的生成结果就一直在识图」）；**别人的图走真识图**
    （没有账本可依）。逃逸口（「不是修改请求 → 输出 reverse」）常开。
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
             mock.patch("app.llm.call_llm",
                        side_effect=describe_side_effect,
                        return_value=llm_reply or seen) as m_text, \
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
        return out, m_describe, m_llm_mod.call_llm, m_gen, m_text

    def test_revision_single_call_sees_image_and_opinion(self):
        # 别人的图 +「手改成插兜」→ 真识图一次，修正后重画（不垫图）。
        out, m_describe, m_llm, m_gen, _ = self._decide(
            "手改成插兜",
            '{"skill": "anima_clear", "prompt": "1girl, hands in pockets"}')
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, hands in pockets",
                                      skill="anima_clear",
                                      _skip_confirm=True)
        m_describe.assert_called_once()          # 别人的图：一次带图调用
        self.assertNotIn("provider",
                         m_describe.call_args.kwargs)  # 跟识图配置走
        sent = m_describe.call_args.kwargs["prompt"]
        self.assertIn("手改成插兜", sent)        # 用户意见进来了
        self.assertIn("忽略此项", sent)          # 没有账本就不给原提示词
        m_llm.assert_not_called()                # 转译链路完全不参与

    def test_edit_opinion_without_verbs_regenerates_t2i(self):
        # 「手怎么多了一根」没有改图动词 → 不垫图，反推修正后重画一张
        out, m_describe, m_llm, m_gen, _ = self._decide(
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

    def test_own_image_uses_ledger_without_vision(self):
        # 2026-10-07 用户拍板：引用**自家 HT 图**改图**不预先识图**——账本里
        # 就是当时真跑的提示词，画面同源；改「发色」这类直接在那段文字上改。
        # 走纯文本调用（`call_llm`），`describe` 一次都不调（省钱）。
        out, m_describe, m_llm, m_gen, m_text = self._decide(
            "手改成插兜",
            seen='{"skill": "hd_3_curvy", "prompt": "miku, hands in pockets"}',
            quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertEqual(out, "")
        m_describe.assert_not_called()           # 自家图不预先识图
        m_text.assert_called_once()              # 纯文本改提示词
        m_llm.assert_not_called()                # 不走 _translate 转译链路
        sent = m_text.call_args[0][0][0]["content"]
        self.assertIn("logged, miku", sent)      # 账本提示词当旁证进场
        self.assertIn("手改成插兜", sent)        # 用户意见进来了
        m_gen.assert_called_once_with("miku, hands in pockets",
                                      skill="hd_3_curvy",
                                      _skip_confirm=True)

    def test_own_image_praise_on_at_round_returns_reverse(self):
        # 自家图 + 夸奖（没点名机制词）+ @ 轮 → 纯文本调用后模型判「不是修改
        # 请求」→ 把 reverse 反推给他
        out, m_describe, m_llm, m_gen, m_text = self._decide(
            "画得真好", at_me=True,
            seen='{"reverse": "miku, blue hair, smiling"}',
            quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("blue hair", out)
        m_describe.assert_not_called()           # 自家图不预先识图
        m_text.assert_called_once()
        m_llm.assert_not_called()
        m_gen.assert_not_called()

    def test_own_image_praise_on_keyword_round_still_replies(self):
        # 自家图 + 夸奖 + 关键词轮 → **照旧把反推发回去**（2026-10-07 改）。
        # 旧行为是「关键词轮闭嘴吞轮」，但群 1103174141 12:02:58 实录证明
        # 那会把一段已生成好的反推整段丢掉（用户报的"后台有日志群里没回复"）。
        out, _, _, m_gen, _ = self._decide(
            "画得真好",
            seen='{"reverse": "miku, blue hair"}',
            quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("miku, blue hair", out)
        m_gen.assert_not_called()

    def test_vision_ask_overrides_ledger(self):
        # 别人的图（账本没中）→ 照旧真识图一次
        out, m_describe, m_llm, m_gen, _ = self._decide(
            "识图 帮我改一下手")
        self.assertEqual(out, "")
        m_describe.assert_called_once()
        m_llm.assert_not_called()                # 不走文本修正
        m_gen.assert_called_once()

    def test_escape_hatch_is_always_open(self):
        """「不是修改请求 → 输出 reverse」这条逃逸口**常开**（2026-10-06）。

        以前代码扫原话：明说「图生图」就把这句从模板里抹掉，等于拿关键词替
        模型判过一次。那套条件拼接（`_ESCAPE_ALLOWED` / `_ESCAPE_FORBIDDEN`）
        已删，两种轮次拿到的模板都带 reverse 说明。
        """
        for text in ("图生图 把头发换成银色", "画得真好"):
            with self.subTest(text=text):
                _, m_describe, _, _, _ = self._decide(
                    text, seen='{"skill": "silver", "prompt": "x"}')
                sent = m_describe.call_args.kwargs["prompt"]
                self.assertIn("reverse 字段", sent)
        for gone in ("_ESCAPE_ALLOWED", "_ESCAPE_FORBIDDEN"):
            self.assertFalse(hasattr(direct_gen, gone), gone + " 又回来了")

    def test_vague_opinion_keeps_escape_hatch(self):
        # 没点名机制词（真闲聊）→ 逃逸口照旧保留，模板里给 reverse 说明
        _, m_describe, _, _, _ = self._decide(
            "画得真好",
            seen='{"reverse": "1girl, solo, blue hair"}')
        sent = m_describe.call_args.kwargs["prompt"]
        self.assertIn("reverse 字段", sent)

    def test_model_reverse_answer_is_believed_even_for_an_i2i_sentence(self):
        """模型判「这轮不是修改请求」就照它：**@ 轮和关键词轮都回反推**。

        2026-10-05 那条「明说图生图就不许回反推文本」是代码否决模型最典型的
        一处——原话命中机制词，就把模型输出的 reverse 硬说成「没解析出来」。
        10-06 拆闸之后它不再有豁免权：判据只写在模板里，结论听模型的。
        10-07 再改：关键词轮不再静默吞掉——能走到这里说明用户确实给了图、
        模型确实读了图，没有理由不发（群实录的反推丢失就是这么来的）。
        """
        out, m_describe, _, m_gen, _ = self._decide(
            "图生图 改动部分异常肢体",
            seen='{"reverse": "这张图的英文tag反推"}', at_me=True)
        m_describe.assert_called_once()
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("这张图的英文tag反推", out)
        m_gen.assert_not_called()

        # 同一条回复在关键词轮里照样发（不刷屏 ≠ 丢回复），也不入队
        out, _, _, m_gen, _ = self._decide(
            "图生图 改动部分异常肢体",
            seen='{"reverse": "这张图的英文tag反推"}', at_me=False)
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("这张图的英文tag反推", out)
        m_gen.assert_not_called()
        m_gen.assert_not_called()

    def test_own_image_bare_at_returns_logged_prompt_zero_calls(self):
        # 自家图 + 裸 @ → 直接回账本提示词，识图和 LLM 都不调
        out, m_describe, m_llm, m_gen, m_text = self._decide(
            "", quoted="编号 HT-20261005-010329-595", at_me=True,
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("logged, miku", out)
        m_describe.assert_not_called()
        m_text.assert_not_called()
        m_llm.assert_not_called()
        m_gen.assert_not_called()

    def test_own_image_with_channel_word_uses_ledger_zero_calls(self):
        # 自家图 +「三档」→ 账本提示词 + 新渠道直接入队，零识图零 LLM
        out, m_describe, m_llm, m_gen, _ = self._decide(
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
        out, m_describe, m_llm, m_gen, _ = self._decide(
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
        out, m_describe, m_llm, m_gen, _ = self._decide(
            "nai", quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "anima_clear",
                        "seed": "414004422"})
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("logged, miku", skill="nai",
                                      _skip_confirm=True)
        self.assertNotIn("seed", m_gen.call_args.kwargs)

    def test_own_image_without_seed_stays_random(self):
        # 老账本记录没有 seed（空串）→ 照旧随机，不传 seed 参数。
        out, m_describe, m_llm, m_gen, _ = self._decide(
            "三档", quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "anima_clear",
                        "seed": ""})
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("logged, miku", skill="hd_3_clear",
                                      _skip_confirm=True)
        self.assertNotIn("seed", m_gen.call_args.kwargs)

    def test_foreign_image_ignores_last_job(self):
        # 引用别人的图：上一轮任务的原提示词绝不进场（锚死事故的根因）
        out, m_describe, _, m_gen, _ = self._decide(
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
        out, m_describe, m_llm, m_gen, _ = self._decide(
            "", seen="1girl, solo, blue hair", at_me=True)
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("blue hair", out)
        self.assertNotIn("provider", m_describe.call_args.kwargs)
        m_llm.assert_not_called()
        m_gen.assert_not_called()

    def test_channel_with_image_generates_from_reverse_zero_llm(self):
        # 引用图 + 只打档位（「三档」）→ 反推后直接生成，零 LLM
        out, m_describe, m_llm, m_gen, _ = self._decide(
            "三档", seen="1girl, solo, blue hair", at_me=True)
        self.assertEqual(out, "")
        m_llm.assert_not_called()
        m_gen.assert_called_once_with("1girl, solo, blue hair",
                                      skill="hd_3_clear", _skip_confirm=True)

    def test_channel_plus_filler_goes_to_ai(self):
        # 2026-10-07：「快档 基于图片帮我生成」这种带渠道词+空话的轮，不再由
        # 代码认死话术（`_GENERIC_I2I_RE` 已删），整句交给 AI 判。
        out, m_describe, m_llm, m_gen, _ = self._decide(
            "快档 基于图片帮我生成",
            seen='{"skill": "hd_fast_clear", "prompt": "1girl, solo, blue hair"}',
            at_me=True)
        self.assertEqual(out, "")
        m_describe.assert_called_once()          # 别人的图 → 识图一次
        sent = m_describe.call_args.kwargs["prompt"]
        self.assertIn("基于图片帮我生成", sent)  # 原话交给 AI
        m_gen.assert_called_once_with("1girl, solo, blue hair",
                                      skill="hd_fast_clear",
                                      _skip_confirm=True)

    def test_praise_on_at_round_returns_reverse_text(self):
        # @ 轮意见不是修改请求 → 一次调用里直接给出反推（不刷菜单、不二调）
        out, m_describe, _, m_gen, _ = self._decide(
            "画得真好", seen='{"reverse": "1girl, solo, blue hair"}',
            at_me=True)
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("blue hair", out)
        m_describe.assert_called_once()
        m_gen.assert_not_called()

    def test_reverse_is_returned_on_keyword_round(self):
        # 关键词轮引用图 + 夸奖（reverse）→ **照样回反推**（2026-10-07 改）。
        # 旧行为是吞成 ""，但那会把已生成的整段反推丢掉（群实录）。关键词轮
        # 也是"用户在叫我"，丢回复不是止刷屏。
        out, _, _, m_gen, _ = self._decide(
            "比大大怪快五秒左右",
            seen='{"reverse": "1girl, solo, blue hair"}')
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("blue hair", out)
        m_gen.assert_not_called()

    def test_revision_unparseable_falls_back_to_terse_tags(self):
        # 模型没吐 JSON 但回的就是一段 tag（≥4 字符纯 ASCII）→ 当成反推发，
        # 不再报「没解析出来」。2026-10-07 群 1103174141 12:02:01 实录：
        # 识图模型回了散文，旧代码直接回错误话术，用户什么都没有。
        out, _, _, m_gen, _ = self._decide("手改成插兜", seen="1girl, blue hair")
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("blue hair", out)
        m_gen.assert_not_called()

    def test_revision_unparseable_non_english_still_errors(self):
        # 又没 JSON、又不像英文 tag（中文散文）→ 仍按"没解析出来"处理，
        # 不许把一段中文散文当提示词发回去。
        out, _, _, m_gen, _ = self._decide(
            "手改成插兜", seen="不是 JSON",
            describe_side_effect=[("不是 JSON"), RuntimeError("429")])
        self.assertIn("没解析出来", out)
        m_gen.assert_not_called()

    def test_revision_call_failure_returns_error(self):
        out, m_describe, _, m_gen, _ = self._decide(
            "手改成插兜", describe_side_effect=RuntimeError("429"))
        self.assertIn("没发出去", out)
        m_describe.assert_called_once()
        m_gen.assert_not_called()


class EnglishDirectTest(unittest.TestCase):
    """英文提示词也过一次 AI（2026-10-05 用户拍板：绝不能绕过 ai）。

    以前「档位 + 英文提示词」和「私聊裸英文」都是零转译直通，副作用是代码
    全文乱搜画风词——`1girl, soft lighting` 被判成 anima_soft，soft 还被从
    正文里删掉。现在一律走 `_translate`。
    """

    def _decide(self, text, llm_reply, at_me=True):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value=llm_reply) as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False, at_me=at_me)
            direct_gen._LAST_JOB.pop("group_1", None)
        return out, m_llm, m_gen

    def test_tier_plus_english_goes_through_ai(self):
        text = "快档 gloss 1girl, solo, blue hair, classroom"
        out, m_llm, m_gen = self._decide(
            text, '{"skill": "hd_fast_gloss", "prompt": '
                  '"1girl, solo, blue hair, classroom"}')
        self.assertEqual(out, "")
        self.assertEqual(m_llm.call_count, 1)
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("用户开头点名了渠道：**hd_fast_gloss**", sent)
        self.assertIn("1girl, solo, blue hair, classroom", sent)
        m_gen.assert_called_once_with("1girl, solo, blue hair, classroom",
                                      skill="hd_fast_gloss",
                                      _skip_confirm=True)

    def test_chinese_desc_still_translated(self):
        # 中文描述照旧走扩写
        out, m_llm, m_gen = self._decide(
            "快档 一只柴犬在草地上",
            '{"skill": "hd_fast_clear", "prompt": "shiba"}')
        self.assertEqual(out, "")
        self.assertEqual(m_llm.call_count, 1)
        m_gen.assert_called_once_with("shiba", skill="hd_fast_clear",
                                      _skip_confirm=True)

    def test_bare_english_private_goes_through_ai(self):
        # 私聊裸发英文提示词（没打渠道词）→ 不再零调用直通，交 AI 判渠道 +
        # 写提示词。
        out, m_llm, m_gen = self._decide(
            "1girl, solo, silver hair, moonlight",
            '{"skill": "anima_clear", "prompt": '
            '"1girl, solo, silver hair, moonlight"}')
        self.assertEqual(out, "")
        self.assertEqual(m_llm.call_count, 1)
        m_gen.assert_called_once_with("1girl, solo, silver hair, moonlight",
                                      skill="anima_clear",
                                      _skip_confirm=True)

    def test_bare_english_keyword_round_now_goes_to_ai(self):
        # 2026-10-05 23:xx：关键词轮不再零调用短路——过一次 AI，AI 判「在
        # 闲聊」就回话（不回菜单）。
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"skill": "", "prompt": "", '
                                            '"reply": "哈哈"}') as m_llm, \
             mock.patch.object(gi, "_generate_image") as m_gen:
            out = direct_gen.decide("just chatting about anime stuff",
                                    [], False, at_me=False)
        self.assertEqual(out, "哈哈")
        self.assertEqual(m_llm.call_count, 1)
        m_gen.assert_not_called()


class MasterPromptTest(unittest.TestCase):
    """唯一模板 `_MASTER_TEMPLATE`：人设 + 工具列表 + 历史 + 用户原话。

    2026-10-05 晚用户拍板重做，原话：「提示词的构成就是：人设是什么？你是一个
    绘图 AI；第二段就是这是你的工具列表，500~600 字，简单写清楚它有什么工具；
    第三段是用户的历史对话；最后一个就是最近的用户需求，就这么简单。」「我
    一直想通过硬编码指令的方式去调用生图工具，但我错了——AI 本身就能胜任整个
    工作。」

    所以这里钉的是**那一条模板里必须有哪几样东西**：
    - 两个工具（generate_image / recall_image）+ 一行 JSON 输出格式
    - 「不用动手就回 {"reply": ...}」——AI 得能聊天，不是只会画图
    - 渠道清单（渠道名是我们自己起的，模型猜不出来的本地事实，必须给）
    - 中文翻英文 / 画师串原样保留 / 新请求不许抄上一轮
    以及**钉死它别再长回去**：模板本身不许超过 1700 字。
    （2026-10-07 从 1600 提到 1700：新增 `silver-hd` 渠道必须在渠道清单里
    占一行——这是模型猜不出来的本地事实，属于合法增长，不是「给渠道写专属规则」。）
    """

    def _rendered(self, text="t", chan_hint="", recent="", search=""):
        return direct_gen._MASTER_TEMPLATE.format(
            recent=recent, text=text, chan_hint=chan_hint, search=search)

    def test_template_has_the_four_sections(self):
        t = self._rendered()
        for part in ("生图 AI", "【工具】", "【渠道 skill】",
                     "【提示词怎么写】", "【最近对话】", "【用户】"):
            with self.subTest(part=part):
                self.assertIn(part, t)

    def test_tool_list_has_exactly_the_two_tools(self):
        t = self._rendered()
        self.assertIn("generate_image", t)
        self.assertIn("recall_image", t)
        # 模板本身要短：一句话一个工具，不给每个渠道写专属规则。
        self.assertLess(len(t), 1700, "模板超长了，工具列表应该一句话一个")

    def test_custom_channels_are_listed_for_the_ai(self):
        """自定义渠道（silver / silver-hd / jank）是本地事实，模型猜不出来，
        必须写进渠道清单，否则 AI 拿到 skill=silver-hd 也不知道那是什么。
        """
        t = self._rendered()
        for ch in ("silver", "silver-hd", "jank"):
            with self.subTest(ch=ch):
                self.assertIn(ch, t)
        # silver-hd 只能点名触发，模板要给出触发词与「否则走 silver」的口径
        self.assertIn("silver 超清", t)

    def test_channel_ids_are_spelled_out_in_words_the_ai_can_map(self):
        """渠道 id 是我们自己起的，用户说的是中文口语，模板必须把两边对上。

        2026-10-05 用户点名：「AI 知道 clear / soft / gloss / curvy 吗？知道。
        但下面这行什么 hd_2_*，AI 它知道这个是什么意思吗？用户说快档、二档、
        三档，AI 知道它说的是什么吗？它知道快档是 hd_fast 吗？用户说用千问去
        跑图，AI 肯定能理解呀。」
        """
        t = self._rendered()
        # 四种画风要有中文对照
        for pair in ("clear 清晰", "soft 柔和", "gloss 油亮", "curvy 肉感"):
            with self.subTest(pair=pair):
                self.assertIn(pair, t)
        # 档位口语 → id 的映射要写死（否则 AI 对不上「二档」）
        self.assertIn("快档", t)
        self.assertIn("hd_fast_", t)
        self.assertIn("二档", t)
        self.assertIn("hd_2_", t)
        self.assertIn("三档", t)
        self.assertIn("hd_3_", t)
        # 固定渠道的中文叫法也要给
        self.assertIn("千问", t)
        self.assertIn("qwen_image_v1", t)
        self.assertIn("通义", t)

    def test_full_prompt_stays_within_budget(self):
        # 用户口径：「2000、3000、4000 字，就这么点」。最坏情况（10 条满长历史）
        # 也不许越过 3000 字。
        hist = [{"role": "user", "content": "描" * 300},
                {"role": "assistant", "content": "[直达生图] nai：" + "x" * 300}] * 5
        t = self._rendered(text="画一只猫", recent=direct_gen._recent_lines(hist))
        self.assertLess(len(t), 3000, "整条提示词越过 3000 字预算了")

    def test_ai_may_reply_instead_of_drawing(self):
        self.assertIn('{"reply"', self._rendered())

    def test_template_carries_the_hard_rules(self):
        t = self._rendered()
        self.assertIn("翻成英文", t)                     # 中文需求要翻译
        self.assertIn("画师串", t)                       # 画师串要原样用上
        self.assertIn("别把上一轮画过的东西抄过来", t)     # 新请求=全新提示词

    def test_channel_list_is_given(self):
        # 渠道名是本地事实，模型猜不出来，必须给。
        t = self._rendered()
        for s in ("anima_clear", "hd_3_", "nai", "qwen_image_v1", "krea2"):
            with self.subTest(skill=s):
                self.assertIn(s, t)

    def test_chan_hint_only_renders_when_code_parsed_a_channel(self):
        self.assertNotIn("用户开头点名了渠道", self._rendered())
        self.assertIn("用户开头点名了渠道：**nai**",
                      self._rendered(chan_hint="\n用户开头点名了渠道：**nai**，"
                                               "就用它。\n"))

    def test_prompt_lang_still_splits_by_channel_for_revise(self):
        # `_prompt_lang` 现在只服务改图模板 `_REVISE_TEMPLATE`，别再删。
        self.assertIn("句子", direct_gen._prompt_lang("qwen_image_v1"))
        for s in ("anima_clear", "hd_3_clear", "nai", "miao"):
            with self.subTest(skill=s):
                self.assertIn("danbooru", direct_gen._prompt_lang(s))

    def test_translate_call_carries_the_master_prompt(self):
        # 端到端：`nai 凯尔希 <逗号画师串>` → 发出去的正文里有画师串规则、
        # 有渠道提示、有用户原话，而且那串英文一个字没丢。
        reply = ('{"tool": "generate_image", "skill": "nai", '
                 '"prompt": "kelthuzad, masterpiece"}')
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value=reply) as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="private_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(gi, "_generate_image", return_value=RECEIPT):
            direct_gen.decide(
                "nai 凯尔希 tianliang_duohe_fangdongye, ciloranko",
                [], False, at_me=True)
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("画师串", sent)
        self.assertIn("用户开头点名了渠道：**nai**", sent)
        self.assertIn("tianliang_duohe_fangdongye", sent)
        self.assertIn("ciloranko", sent)

    def test_reply_output_is_sent_as_a_message(self):
        # 模型选择不动手（聊天 / 问画师串）→ 那句话原样发出去，**一张图都不入队**。
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"reply": "我现在没存画师串。"}'
                               ) as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="private_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(gi, "_generate_image") as m_gen:
            out = direct_gen.decide("你知道我们应该用哪些画师串吗", [],
                                    False, at_me=True)
        self.assertEqual(out, "我现在没存画师串。")
        m_gen.assert_not_called()


class VerbatimWeightedPromptTest(unittest.TestCase):
    """成品串轮（NAI 权号 `::`）：过一次 AI + 渠道「点名词优先 / AI 判 / 代码校验」。

    2026-10-05 私聊 2509355624 实录立下的契约：半角冒号不是署名分隔符、
    权重 tag 里的 `soft_focus` 不算画风词、开头点名的渠道不许被模型复议。
    当晚用户加码「ai 必须参与决策，绝对不能绕过 ai」→ 成品串不再零转译直通，
    改成过一次 AI；再往后（当晚 23:xx）四条模板合并成唯一一条
    `_MASTER_TEMPLATE`，权号规则写在它的「提示词怎么写」段里。
    """

    SAMPLE = ("nai 9:16 1.05::light_rays::, 0.8::soft_focus::, "
              "0.8::_depth_of_field::\n\nmasterpiece, best_quality, "
              "1girl, izumi_konata, pov\n\n| girl,")
    BODY = SAMPLE[len("nai "):]

    def _decide(self, text, llm_reply=None, boom=False, at_me=True):
        patched = ({"side_effect": RuntimeError("429")} if boom
                   else {"return_value": llm_reply})
        with mock.patch.object(direct_gen.llm, "call_llm", **patched) as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="private_1"), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False, at_me=at_me)
            direct_gen._LAST_JOB.pop("private_1", None)
        return out, m_llm, m_gen

    def _echo(self, skill, body):
        """模型「听话」时的回复：渠道照抄、正文原样保留。"""
        return json.dumps({"skill": skill, "prompt": body})

    def test_named_channel_locks_channel_and_keeps_weights(self):
        # 开头点了 nai → 渠道锁死交给 AI；模板里写着权号/画师串「原样保留，
        # 别翻译、别删、别改成平铺 tag」，正文（含换行、结尾逗号）原样带过去。
        out, m_llm, m_gen = self._decide(self.SAMPLE,
                                         llm_reply=self._echo("nai", self.BODY))
        self.assertEqual(out, "")
        self.assertEqual(m_llm.call_count, 1)
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("用户开头点名了渠道：**nai**", sent)
        self.assertIn("原样保留", sent)
        self.assertIn("::", sent)
        m_gen.assert_called_once_with(self.BODY, skill="nai",
                                      _skip_confirm=True)
        self.assertIn("\n\n", m_gen.call_args[0][0])

    def test_group_signature_does_not_eat_the_prompt(self):
        # 根因回归：署名正则以前认半角 `:`，把 `nai 9:16 1.05::…` 从第一个
        # 冒号切了一刀，渠道词连同开头一起被吃掉。
        self.assertEqual(direct_gen._strip_attribution(self.SAMPLE),
                         self.SAMPLE)
        out, _m, m_gen = self._decide("胡桃桃：" + self.SAMPLE,
                                      llm_reply=self._echo("nai", self.BODY))
        self.assertEqual(out, "")
        m_gen.assert_called_once_with(self.BODY, skill="nai",
                                      _skip_confirm=True)

    def test_weight_tag_words_are_not_style_words(self):
        # `0.8::soft_focus::` 里的 soft 不是画风词（下划线也得挡）。
        skill, _desc = _pc("三档 0.8::soft_focus:: 猫")
        self.assertEqual(skill, "hd_3_clear")

    def test_unprefixed_prompt_lets_ai_judge(self):
        # 没点渠道词 → 交给模型判；判出来就入队。
        out, m_llm, m_gen = self._decide(
            self.BODY, llm_reply=self._echo("nai", self.BODY))
        self.assertEqual(out, "")
        self.assertEqual(m_llm.call_count, 1)
        m_gen.assert_called_once_with(self.BODY, skill="nai",
                                      _skip_confirm=True)

    def test_ai_cannot_overrule_the_named_channel(self):
        # 点名词在场 → 代码锁死；模型就算回别的渠道也不采纳。
        out, m_llm, m_gen = self._decide(
            self.SAMPLE, llm_reply=self._echo("anima_clear", self.BODY))
        self.assertEqual(out, "")
        self.assertEqual(m_llm.call_count, 1)
        self.assertEqual(m_gen.call_args.kwargs["skill"], "nai")

    def test_unjudgeable_channel_asks_and_never_enqueues(self):
        for reply in ('{"skill": "", "prompt": "x"}',
                      '{"skill": "midjourney", "prompt": "x"}'):
            with self.subTest(reply=reply):
                out, _m, m_gen = self._decide(self.BODY, llm_reply=reply)
                self.assertEqual(out, direct_gen._FINAL_PROMPT_NO_CHAN_TEXT)
                self.assertIn("渠道", out)
                m_gen.assert_not_called()

    def test_llm_failure_never_falls_back_to_default_channel(self):
        # 模型挂了也不静默换默认档烧一张错风味的图——回问，零入队。
        out, _m, m_gen = self._decide(self.BODY, boom=True)
        self.assertEqual(out, direct_gen._FINAL_PROMPT_NO_CHAN_TEXT)
        m_gen.assert_not_called()

    def test_tier_prefix_stripped_weights_kept(self):
        # 「三档 1.1::x::」：档位词是指令（代码认出来 → hd_3_clear），抠掉；
        # 权号正文原样交给 AI。
        out, m_llm, m_gen = self._decide(
            "三档 1.1::x::, -1::y::",
            llm_reply=self._echo("hd_3_clear", "1.1::x::, -1::y::"))
        self.assertEqual(out, "")
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("用户开头点名了渠道：**hd_3_clear**", sent)
        self.assertIn("1.1::x::, -1::y::", sent)
        m_gen.assert_called_once_with("1.1::x::, -1::y::",
                                      skill="hd_3_clear",
                                      _skip_confirm=True)

    def test_channel_word_inside_the_body_is_not_a_command(self):
        # 串正文里的 `nai`/`nffa` 是 tag，不是点名词——不能从中间把提示词剪断。
        self.assertEqual(
            direct_gen._named_channel("1.1::artist:okonogi_nai:: , 1girl"),
            (None, "1.1::artist:okonogi_nai:: , 1girl"))
        self.assertEqual(direct_gen._named_channel("solo, anime_nffa_1")[0],
                         None)

    def test_negative_weights_keep_their_minus(self):
        skill, body = direct_gen._named_channel("nai -1::bad_hand::, 1girl")
        self.assertEqual(skill, "nai")
        self.assertEqual(body, "-1::bad_hand::, 1girl")

    def test_channel_word_with_no_body_asks_for_the_prompt(self):
        out, _m, m_gen = self._decide("nai ::")
        self.assertIn("正文是空的", out)
        m_gen.assert_not_called()

    def test_keyword_round_goes_to_ai_and_ai_decides(self):
        # 2026-10-05 23:xx：群里别人贴一段串讨论（没 @、没点渠道词）也交给
        # AI 判——AI 判「不是下单」就回话，不再由代码按「有没有点名渠道词」
        # 一刀切回菜单。
        out, m_llm, m_gen = self._decide(
            self.BODY,
            llm_reply='{"skill": "", "prompt": "", '
                      '"reply": "这段串看着像 NAI 的写法"}',
            at_me=False)
        self.assertEqual(out, "这段串看着像 NAI 的写法")
        self.assertEqual(m_llm.call_count, 1)
        m_gen.assert_not_called()
        # 他自己写了渠道词 → 走成品串分支，照样过一次 AI 出图
        out, m_llm, m_gen = self._decide(
            self.SAMPLE, llm_reply=self._echo("nai", self.BODY), at_me=False)
        self.assertEqual(out, "")
        m_gen.assert_called_once_with(self.BODY, skill="nai",
                                      _skip_confirm=True)

    def test_translate_never_lets_the_model_veto_a_named_channel(self):
        # 转译轮同一套优先级：代码点名 > 模型判的（以前是「模型值 or 代码值」,
        # 等于把用户点名的渠道交给模型复议）。
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"skill": "anima_clear", '
                                            '"prompt": "cat"}'):
            got = direct_gen._translate("画猫", [], skill="nai")
        self.assertEqual(got["skill"], "nai")
        # 代码没点名时才用模型的判据；模型乱填且没点名词 → 才落默认档
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"skill": "gpt-image", '
                                            '"prompt": "cat"}'):
            got = direct_gen._translate("画猫", [])
        self.assertEqual(got["skill"], direct_gen._DEFAULT_SKILL)


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
    """看图说话：@ 轮 + 引用图 + 描述类问题 → 2026-10-07 起交给**改图管道**。

    原先代码认死「画的什么 / 什么画风」这类话术直调识图（`_DESCRIBE_RE`），
    已删。现在所有「引用图 + 有话说」统一进 `_revise`：模型判这轮不是修改
    请求就回一段反推文本（`reverse`），判是改图就出提示词。别人的图走识图。

    这里钉住两件事：① 「这画的是什么」仍能得到一句中文/文本回答（不再由
    代码直调识图，而是走 AI）；② 渠道词在场时照样进改图管道，不能因为
    「提问」就把渠道词丢了。
    """

    def _decide(self, text, urls=("img1",), at_me=True,
                describe_reply='{"reverse": "红色的正方形。"}', quoted="",
                lookup_row=None, describe_side_effect=None):
        with mock.patch.object(direct_gen, "llm") as m_llm_mod, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=quoted), \
             mock.patch("app.vision.describe",
                        side_effect=describe_side_effect,
                        return_value=describe_reply) as m_describe, \
             mock.patch.object(direct_gen.image_log, "lookup",
                               return_value=lookup_row), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False, data_urls=list(urls),
                                    at_me=at_me)
        return out, m_describe, m_gen, m_llm_mod

    def test_describe_question_goes_through_revise(self):
        # 没渠道词 + 描述类问题 → 改图管道，模型判不是修改请求 → 回反推文本
        out, m_describe, m_gen, _ = self._decide("这画的是什么？")
        self.assertIn(direct_gen._REVERSE_HEADER, out)
        self.assertIn("红色的正方形", out)
        m_describe.assert_called_once()          # 别人的图仍识图一次
        m_gen.assert_not_called()

    def test_describe_with_channel_falls_back_to_tags(self):
        # 渠道词在场 → 走改图管道（模型在这一次调用里判）。模型没吐 JSON、
        # 又不是英文 tag → 用识图模型自己的提示词兜一次反推（2026-10-07
        # 新增的兜底）。这里让兜底那次返回真实英文 tag。
        out, m_describe, m_gen, _ = self._decide(
            "sd 这画的是什么",
            describe_side_effect=["不是 JSON", "1girl, solo, blue hair"])
        self.assertIn("blue hair", out)
        self.assertEqual(m_describe.call_count, 2)   # 主调用 + 反推兜底
        m_gen.assert_not_called()


class ReverseTriggerTest(unittest.TestCase):
    """「反推提示词」硬触发（2026-10-07 用户拍板，零 AI 调用）。

    背景（群 1103174141 实测两条 bug）：带图 +「反推提示词」以前被塞进改图
    管道 `_revise` 的 JSON 模板（主人设），结果 ① 模型回散文 → 报「改图请求
    没解析出来」；② 关键词轮 JSON 解析成功也被 `at_me=False` 静默丢掉。
    现在明确命中就直走识图模型自己的提示词，出**纯英文 tag**、原样发。
    """

    def _decide(self, text, urls=("img1",), at_me=False, quoted="",
                describe_reply="1girl, solo, blue hair",
                lookup_row=None):
        with mock.patch.object(direct_gen, "llm") as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=quoted), \
             mock.patch("app.vision.describe",
                        return_value=describe_reply) as m_describe, \
             mock.patch.object(direct_gen.image_log, "lookup",
                               return_value=lookup_row), \
             mock.patch.object(direct_gen, "_prefetch_search",
                               return_value=""), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            # AI 那条路桩成"不接管"：没带图时 decide 会落到 _translate。
            m_llm.call_llm.return_value = "{}"
            out = direct_gen.decide(text, [], False, data_urls=list(urls),
                                    at_me=at_me)
        return out, m_describe, m_gen, m_llm

    def test_trigger_returns_english_tags_verbatim(self):
        out, m_describe, m_gen, m_llm = self._decide("反推提示词")
        self.assertEqual(out, "反推的是：\n1girl, solo, blue hair")
        m_describe.assert_called_once()
        m_llm.assert_not_called()          # 零 AI 调用
        m_gen.assert_not_called()

    def test_trigger_uses_vision_own_prompt_not_persona(self):
        # 发给识图模型的是 _TAGS_PROMPT（识图专用），不是主人设模板。
        _, m_describe, _, _ = self._decide("反推提示词")
        sent = m_describe.call_args.kwargs["prompt"]
        self.assertEqual(sent, direct_gen._TAGS_PROMPT)
        self.assertNotIn("reverse 字段", sent)

    def test_keyword_round_reverse_is_sent(self):
        # 关键词轮（at_me=False）**照样**把反推发出去——这正是 bug ② 的修复点。
        out, _, _, _ = self._decide("大大怪 ，反推提示词", at_me=False)
        self.assertIn("blue hair", out)

    def test_upstream_refusal_is_not_sent_as_reverse_text(self):
        """上游拒答**不许**当反推结果发出去（2026-10-07 修）。

        小米 MiMo 碰到 NSFW 回一句英文散文，实测一天 7 次被加了「反推的是：」
        前缀发给了用户；会话记录里实锤：
        {"x": "反推的是： The request was rejected because it was considered high risk"}
        """
        out, _, _, _ = self._decide(
            "反推提示词",
            describe_reply="The request was rejected because it was considered high risk")
        self.assertEqual(out, direct_gen._REVERSE_REFUSED_TEXT)
        self.assertNotIn("rejected", out)
        self.assertNotIn(direct_gen._REVERSE_HEADER, out)

    def test_chinese_policy_refusal_is_not_sent_as_reverse_text(self):
        out, _, _, _ = self._decide(
            "反推提示词",
            describe_reply="该内容涉及色情低俗信息，不符合公序良俗和相关规范，"
                           "我不能按照你的要求进行描述。")
        self.assertEqual(out, direct_gen._REVERSE_REFUSED_TEXT)
        self.assertNotIn("公序良俗", out)

    def test_real_world_phrasings_hit(self):
        for text in ("反推提示词", "大大怪，反推提示词，然后生成",
                     "识别图片，反推提示词", "反推一下这张图", "反推"):
            with self.subTest(text=text):
                out, _, _, _ = self._decide(text)
                self.assertIn(direct_gen._REVERSE_HEADER, out)

    def test_ledger_wins_when_own_image(self):
        # 引用自家图（账本命中）→ 直接发当时的真实提示词，不劳识图模型。
        out, m_describe, _, _ = self._decide(
            "反推提示词", quoted="编号 HT-20261005-010329-595",
            lookup_row={"prompt": "logged, miku", "skill": "hd_3_curvy"})
        self.assertIn("logged, miku", out)
        m_describe.assert_not_called()

    def test_no_image_falls_through_to_ai(self):
        # 只打了「反推提示词」没带图 → 不劫这轮，交回 AI（它会问哪张图）。
        out, m_describe, _, m_llm = self._decide("反推提示词", urls=())
        m_describe.assert_not_called()
        # 关键断言：没被反推分支劫走 —— 走的是 AI 转译（llm 被调用）。
        m_llm.call_llm.assert_called()

    def test_plain_chatter_is_not_hijacked(self):
        for text in ("识别图片", "这画的是什么", "反推的不对", "提取提示词"):
            with self.subTest(text=text):
                self.assertIsNone(direct_gen._REVERSE_RE.match(text), text)


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


class BareImageTest(unittest.TestCase):
    """裸图（只发图、一个字没说）绝不跑图（2026-10-05 用户拍板）。

    纯图消息的正文不是空串而是「[图片]」占位符——16:33 私聊实录：占位符
    被当成真话掉进改图管道，识图完直接入队生图了。归一成空文本走「空文本
    +图 → 反推」分支；占位符后面带真实意见的不受影响。
    """

    def _decide(self, text, at_me=True, seen="1girl, smile"):
        with mock.patch.object(direct_gen, "llm") as _m_llm_mod, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="private_9"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""), \
             mock.patch("app.vision.describe",
                        return_value=seen) as m_describe, \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False,
                                    data_urls=["data:image/jpeg;base64,A"],
                                    at_me=at_me)
        return out, m_describe, m_gen

    def test_placeholder_only_image_reverse_not_generate(self):
        out, m_describe, m_gen = self._decide("[图片]")
        m_gen.assert_not_called()                # 绝不入队
        m_describe.assert_called_once()          # 反推只花一次识图
        self.assertIn("反推", out)

    def test_multiple_placeholders_still_reverse(self):
        out, _m_describe, m_gen = self._decide("[图片] [图片] ")
        m_gen.assert_not_called()
        self.assertIn("反推", out)

    def test_image_plus_real_opinion_still_generates(self):
        # 图 + 真实意见（「[图片] 手改成插兜」是带图消息的正文形态）→
        # 照旧走改图管道，归一规则不能把真话一起吞掉
        out, m_describe, m_gen = self._decide(
            "[图片] 手改成插兜",
            seen='{"skill": "anima_clear", "prompt": "1girl, fixed"}')
        self.assertEqual(out, "")
        m_gen.assert_called_once()
        m_describe.assert_called_once()


class UnifiedIntentTest(unittest.TestCase):
    """私聊/群聊统一：一次 `_translate` 判意图（2026-10-05 23:xx 用户拍板）。

    用户原话：「用户问大大怪你好，那大大怪就给他回复…我让它生图，那 AI 它
    肯定能够理解的呀，工具我都已经给它了」「正常交流就行了，问一次回一次」。
    → 不再有独立的私聊问答模板/分支，群聊也不再回菜单。
    """

    def _decide(self, text, history=None, key="private_9", at_me=True,
                llm_reply='{"skill": "", "prompt": "", "reply": "大大怪在呢"}'):
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
        self.assertEqual(out, "大大怪在呢")
        m_llm.assert_called_once()              # 单次调用，无循环
        m_gen.assert_not_called()
        msgs = m_llm.call_args.args[0]
        # 唯一模板：单条 user 消息（人设 + 工具 + 历史 + 原话都在里面），
        # 不再有独立的 system 问答人设。
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertIn("你知道NovelAI是什么吗？", msgs[0]["content"])
        # 回复进了落史暂存，qq_bot 送出后 pop 落史
        self.assertEqual(direct_gen.pop_qa("private_9"),
                         ("你知道NovelAI是什么吗？", "大大怪在呢"))

    def test_greeting_gets_reply(self):
        # 「大大怪你好」→ 回话（用户点名的例子）
        out, m_llm, m_gen = self._decide("大大怪你好", at_me=False)
        self.assertEqual(out, "大大怪在呢")
        m_gen.assert_not_called()

    def test_group_question_now_replies_not_menu(self):
        # 2026-10-05 23:xx：群聊不再有「生图或菜单」铁律——问句照样回话。
        out, m_llm, m_gen = self._decide("你知道NovelAI是什么吗？",
                                         key="group_1", at_me=False)
        self.assertEqual(out, "大大怪在呢")
        m_llm.assert_called_once()
        m_gen.assert_not_called()

    def test_english_question_not_treated_as_prompt(self):
        # 「who are you?」是英文问句 → AI 回话，不该被拿去生图
        out, m_llm, m_gen = self._decide("who are you?")
        self.assertEqual(out, "大大怪在呢")
        m_gen.assert_not_called()
        m_llm.assert_called_once()

    def test_drawing_intent_still_generates(self):
        # 「大大怪 画一只猫」→ AI 判「要画图」→ 走生图
        out, m_llm, m_gen = self._decide(
            "大大怪 画一只猫",
            llm_reply='{"skill": "anima_clear", "prompt": "cat"}')
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("cat", skill="anima_clear",
                                      _skip_confirm=True)

    def test_named_channel_drawing_still_generates(self):
        # 「大大怪帮我用 qwen 画一个图」→ 点名渠道 → 走生图（用户点名的例子）
        out, m_llm, m_gen = self._decide(
            "大大怪帮我用 qwen 画一个图",
            llm_reply='{"skill": "qwen_image_v1", "prompt": "a cat"}')
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_args.kwargs["skill"], "qwen_image_v1")

    def test_history_passed_into_master_template(self):
        hist = [{"role": "system", "content": "head"},
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"}]
        out, m_llm, _m_gen = self._decide("还在吗？", history=hist)
        self.assertEqual(out, "大大怪在呢")
        sent = m_llm.call_args.args[0][0]["content"]
        self.assertIn("q1", sent)               # 历史进了模板
        self.assertNotIn("head", sent)          # system 头被滤掉

    def test_llm_failure_not_taken_over(self):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               side_effect=RuntimeError("down")), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="private_9"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=""):
            out = direct_gen.decide("在吗？", [], False, at_me=True)
            self.addCleanup(direct_gen.pop_qa, "private_9")
        self.assertIsNone(out)


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
        self.assertEqual(m_gen.call_args.kwargs["skill"],
                         direct_gen._DEFAULT_SKILL)

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
        # 2026-10-05 用户口径：按**关键词**识别——带全角冒号、夹在别的话里都算，
        # 不再要求整条精确匹配（用户实际发过「：更多渠道」掉进了兜底）。
        for t in ("/更多渠道", "更多渠道", "：更多渠道", "／更多渠道",
                  "大大怪 更多渠道", "还有更多渠道吗"):
            with self.subTest(t=t):
                out, m_gen, _m_llm = self._decide(t)
                self.assertEqual(out, direct_gen.MORE_CHAN_TEXT)
                m_gen.assert_not_called()

    def test_cunny_listed_in_both_menus(self):
        # 2026-10-05：cunny 渠道要同时出现在「使用指南」和「/更多渠道」里。
        self.assertIn("cunny", direct_gen.GUIDE_TEXT)
        self.assertIn("cunny", direct_gen.MORE_CHAN_TEXT)

    def test_miao_listed_in_both_menus(self):
        # 2026-10-05：miao 渠道同样要两个菜单都有。
        self.assertIn("miao", direct_gen.GUIDE_TEXT)
        self.assertIn("miao", direct_gen.MORE_CHAN_TEXT)

    def test_direct_prefix_documented_in_menus(self):
        # 2026-10-07 用户拍板：/直通- 用法要两个菜单都写清楚。
        for menu in (direct_gen.MORE_CHAN_TEXT, direct_gen.GUIDE_TEXT):
            self.assertIn("/直通-", menu)
            self.assertIn("直通", menu)
            self.assertIn("|", menu)

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


class DirectPrefixTest(unittest.TestCase):
    """/直通- 前缀：严格匹配、零 token 直出（2026-10-07 用户拍板）。

    格式 `/直通-渠道|英文提示词`；渠道认不出 → 落回原管道（交 AI），不猜。
    """

    def _decide(self, text, llm_reply='{"skill": "anima_clear", "prompt": "x"}',
                quoted="", data_urls=None):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value=llm_reply) as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=quoted), \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False, at_me=True,
                                    data_urls=data_urls)
            self.addCleanup(direct_gen._LAST_JOB.pop, "group_1", None)
        return out, m_llm, m_gen

    def test_silver_prefix_direct(self):
        out, m_llm, m_gen = self._decide("/直通-silver|1girl, blue hair")
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, blue hair", skill="silver",
                                      _skip_confirm=True)
        m_llm.assert_not_called()           # 零 LLM

    def test_tier_style_prefix_direct(self):
        out, m_llm, m_gen = self._decide("/直通-三档 gloss|1girl, blue hair")
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, blue hair", skill="hd_3_gloss",
                                      _skip_confirm=True)
        m_llm.assert_not_called()

    def test_named_channel_prefix_direct(self):
        out, m_llm, m_gen = self._decide("/直通-qwen|1girl, blue hair")
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, blue hair",
                                      skill="qwen_image_v1", _skip_confirm=True)
        m_llm.assert_not_called()

    def test_prompt_verbatim_with_dash_and_weight(self):
        # 提示词里的 `-`、`::` 权号一律不碰（这就是不用 `-` 当分隔符的理由）。
        prompt = "1girl-blue hair, -1::lowres::"
        out, m_llm, m_gen = self._decide("/直通-silver|" + prompt)
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_args.args[0], prompt)

    def test_fullwidth_slash_works(self):
        out, _m_llm, m_gen = self._decide("／直通-silver|1girl")
        self.assertEqual(out, "")
        m_gen.assert_called_once()

    def test_unknown_channel_falls_back_to_ai(self):
        # 渠道认不出 → 不直通，落回原管道（m_llm 被调用，m_gen 用 AI 给的 skill）。
        out, m_llm, m_gen = self._decide("/直通-xyz|1girl",
                                         llm_reply='{"skill": "anima_clear",'
                                                   ' "prompt": "1girl"}')
        m_llm.assert_called()
        m_gen.assert_called_once_with("1girl", skill="anima_clear",
                                      _skip_confirm=True)

    def test_missing_bar_returns_hint(self):
        out, m_llm, m_gen = self._decide("/直通-silver-1girl, blue hair")
        self.assertIn("/直通-", out)
        m_gen.assert_not_called()
        m_llm.assert_not_called()

    def test_empty_after_head_returns_hint(self):
        out, m_llm, m_gen = self._decide("/直通-")
        self.assertIn("直通", out)
        m_gen.assert_not_called()
        m_llm.assert_not_called()

    def test_empty_prompt_returns_hint(self):
        out, m_llm, m_gen = self._decide("/直通-silver|")
        self.assertIn("提示词", out)
        m_gen.assert_not_called()
        m_llm.assert_not_called()

    def test_no_prefix_not_affected(self):
        # 不带前缀的普通消息照旧走 AI（前缀是唯一开关）。
        out, m_llm, m_gen = self._decide("silver 1girl, blue hair",
                                         llm_reply='{"skill": "silver",'
                                                   ' "prompt": "1girl"}')
        m_llm.assert_called()

    def test_gate_treats_prefix_as_at_me(self):
        ok, why = qq_bot._should_reply({"user_id": "1"}, "group", "9",
                                       "/直通-silver|1girl", at_me=False)
        self.assertTrue(ok)
        self.assertEqual(why, "直通前缀")

    def test_prefix_with_image_passes_source_image(self):
        # 引用图 + /直通-qwen → 垫这张图（qwen 图生图靠它绕过去）。
        _out, _m_llm, m_gen = self._decide("/直通-qwen|change hair to silver")
        self.assertNotIn("source_image", m_gen.call_args.kwargs)  # 无图轮
        _out2, _m_llm2, m_gen2 = self._decide(
            "/直通-qwen|change hair to silver", data_urls=["img1"])
        self.assertEqual(m_gen2.call_args.kwargs.get("source_image"), "1")


class PromptAskNarrowTest(unittest.TestCase):
    """「提取提示词」直通**只认这 5 个字**（2026-10-07 用户拍板）。

    历史：原先是两条正则（`_PROMPT_ASK_RE` 动词+提示词/词条、`_PROMPT_BARE_RE`
    只认裸「提示词」）。用户 2026-10-07 拍板全删：「我就只需要这一句话就行了…
    提取提示词…它只需要这 5 个字，其他的全部给我删掉，其他的全部都给我跑 AI」。
    理由是正则永远盖不全用户的说法（「我要这个图片的提示词」「这个提示词是
    什么」…），而大模型能覆盖 99%。

    所以这个类钉：**只有裸「提取提示词」命中账本直通**，其余一律走 AI。
    """

    def _decide(self, text, quoted="", data_urls=None, lookup_row=None,
                describe_reply="1girl, reversed, tags",
                llm_reply='{"skill": "anima_clear", "prompt": "1girl, cat"}'):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value=llm_reply) as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=quoted), \
             mock.patch("app.vision.describe",
                        return_value=describe_reply) as m_describe, \
             mock.patch.object(direct_gen.image_log, "lookup",
                               return_value=lookup_row), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False,
                                    data_urls=data_urls, at_me=False)
            direct_gen._LAST_JOB.pop("group_1", None)
        return out, m_describe, m_llm, m_gen

    def test_only_extract_prompt_hits(self):
        """只认裸「提取提示词」（可带标点/空格）；别的说法一律不中。"""
        hits = ("提取提示词", "提取提示词。", " 提取提示词！")
        for text in hits:
            self.assertTrue(direct_gen._PROMPT_BARE_RE.match(text), text)
        misses = ("提示词不变，角色换成花火，手势改成抓手",
                  "把提示词里的衣服改成红色", "不要提示词，直接画",
                  "这个提示词是什么", "silver 生成", "提示词", "给我提示词",
                  "查看提示词", "词条发我一份", "我要这个图片的提示词")
        for text in misses:
            self.assertIsNone(direct_gen._PROMPT_BARE_RE.match(text), text)
        self.assertFalse(hasattr(direct_gen, "_PROMPT_ASK_RE"),
                         "_PROMPT_ASK_RE 又回来了（用户明确要求删掉）")

    def test_quote_with_edit_instruction_reaches_the_revise_pipeline(self):
        """233 群那条原话：带「提示词」但其实是改图请求 → 不被吞。

        账本中的是自家图 → 走纯文本改图（`_revise` 不预先识图），原话照样交给
        AI 判；这里钉住「没被直通劫走」（不返回账本原文）。
        """
        out, m_describe, m_llm, m_gen = self._decide(
            "提示词不变，角色换成花火，手势改成抓手",
            quoted="HT-20261004-155628-040 · 728×1024 · anima_clear · "
                   "seed 2226632694",
            data_urls=["data:image/jpeg;base64,A"],
            lookup_row={"prompt": "1girl, purple twin drills",
                        "skill": "anima_clear"})
        m_gen.assert_called_once()                       # 走 AI 判 → 生图
        self.assertNotIn("这张图当初用的提示词", out)   # 没被直通劫走

    def test_ledger_prompt_still_reaches_the_model(self):
        """账本提示词没被丢——它作为「最可信旁证」进改图/反推那次调用。

        自家图 → 纯文本调用（`call_llm`），账本原文进 prompt。
        """
        _out, _d, m_llm, _gen = self._decide(
            "这个提示词是什么",
            quoted="HT-20261004-155628-040 · anima_clear · seed 1",
            data_urls=["data:image/jpeg;base64,A"],
            lookup_row={"prompt": "1girl, purple twin drills",
                        "skill": "anima_clear"})
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("1girl, purple twin drills", sent)

    def test_without_quote_not_hijacked(self):
        # 没引用 +「帮我写提示词 画一只猫」→ 照旧走转译生图，不被劫走
        out, m_describe, m_llm, m_gen = self._decide("帮我写提示词 画一只猫")
        self.assertEqual(out, "")
        m_gen.assert_called_once()
        self.assertEqual(m_gen.call_args.args[0], "1girl, cat")  # 转译结果


class ViewPromptKeywordTest(unittest.TestCase):
    """「提取提示词」直通（2026-10-07 收窄）：命中 → 直接发账本提示词，零 LLM。

    只认裸「提取提示词」5 个字。查哪张图靠 `_ledger_hit`（从引用正文+原话抽
    HT 编号查账本）；账本没中就不劫这轮（交回下面的流程）。
    """

    def _decide(self, text, quoted="", data_urls=None, lookup_row=None,
                describe_reply="1girl, reversed, tags",
                llm_reply='{"skill": "anima_clear", "prompt": "1girl, cat"}'):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value=llm_reply) as m_llm, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch.object(direct_gen.qq_api, "current_quoted_text",
                               return_value=quoted), \
             mock.patch("app.vision.describe",
                        return_value=describe_reply) as m_describe, \
             mock.patch.object(direct_gen.image_log, "lookup",
                               return_value=lookup_row), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, [], False,
                                    data_urls=data_urls, at_me=False)
            direct_gen._LAST_JOB.pop("group_1", None)
        return out, m_describe, m_llm, m_gen

    def test_keyword_returns_ledger_prompt_without_llm(self):
        """引用带 HT 编号的图 +「提取提示词」→ 直接回账本提示词，零 LLM。"""
        out, m_describe, m_llm, m_gen = self._decide(
            "提取提示词",
            quoted="HT-20261004-155628-040 · 728×1024 · anima_clear · "
                   "seed 2226632694",
            lookup_row={"prompt": "1girl, purple twin drills",
                        "skill": "anima_clear", "seed": "2226632694"})
        self.assertIn("1girl, purple twin drills", out)
        m_llm.assert_not_called()
        m_describe.assert_not_called()
        m_gen.assert_not_called()

    def test_keyword_in_text_also_works(self):
        """HT 编号直接写在引用正文里（没引用）也能查到。"""
        out, _d, _l, _g = self._decide(
            "提取提示词",
            quoted="HT-20261004-155628-040 · anima_clear · seed 1",
            lookup_row={"prompt": "1girl, test", "skill": "anima_clear"})
        self.assertIn("1girl, test", out)

    def test_plain_prompt_question_goes_to_ai(self):
        """只有「提示词」不含「提取提示词」→ 不触发直通，落进引用图轮（走 AI）。

        账本命中的是**自家图** → 走纯文本改图（`_revise` 不预先识图），所以
        describe 不调、账本原文当旁证进 AI 调用。
        """
        out, m_describe, m_llm, m_gen = self._decide(
            "这个提示词是什么",
            quoted="HT-20261004-155628-040 · anima_clear · seed 1",
            data_urls=["data:image/jpeg;base64,A"],
            lookup_row={"prompt": "1girl, purple twin drills",
                        "skill": "anima_clear"})
        m_describe.assert_not_called()   # 自家图 → 不预先识图

    def test_bare_keyword_without_ledger_hit_is_not_hijacked(self):
        """「提取提示词」但引用的不是自家图（账本没中）→ 不劫这轮，照常看图反推。"""
        out, m_describe, _l, m_gen = self._decide(
            "提取提示词", quoted="随便一张别人发的图",
            data_urls=["data:image/jpeg;base64,A"], lookup_row=None)
        m_describe.assert_called_once()

    def test_extract_keyword_returns_ledger_prompt(self):
        """实打「提取提示词」+ 引用带编号 → 回账本提示词，一个调用都不烧。"""
        out, m_describe, m_llm, m_gen = self._decide(
            "提取提示词",
            quoted="HT-20261006-191917-458 · 2048×3072 · jank · seed 4119332997",
            data_urls=["data:image/jpeg;base64,A"],
            lookup_row={"prompt": "izumi_sagiri, 1girl, solo",
                        "skill": "jank", "seed": "4119332997"})
        self.assertIn("izumi_sagiri, 1girl, solo", out)
        self.assertIn("jank", out)
        m_describe.assert_not_called()      # 绝不重新识图跑图
        m_gen.assert_not_called()
        m_llm.assert_not_called()

    def test_other_wordings_go_to_ai(self):
        """2026-10-07 用户拍板：只有「提取提示词」直通，其余说法全部走 AI。

        「给我提示词 / 我要提示词 / 词条发我一份」这类不再零调用直通——它们
        会落进引用图轮的改图管道（自家图走纯文本修正、别人的图识图），由 AI
        判是不是在要词条。这里钉住「不被直通劫走」。
        """
        for text in ("给我提示词", "提取一下提示词", "我要提示词",
                     "发我提示词", "提示词给我", "词条发我一份", "提示词。"):
            out, m_describe, _l, m_gen = self._decide(
                text,
                quoted="HT-20261006-191917-458 · jank · seed 1",
                data_urls=["data:image/jpeg;base64,A"],
                lookup_row={"prompt": "1girl, kept", "skill": "jank"})
            self.assertNotIn("1girl, kept", out, text)   # 不再零调用直通

    def test_bare_keyword_without_ledger_hit_is_not_hijacked(self):
        """「提取提示词」但引用的不是自家图（账本没中）→ 不劫这轮，照常看图反推。"""
        out, m_describe, _l, m_gen = self._decide(
            "提取提示词", quoted="随便一张别人发的图",
            data_urls=["data:image/jpeg;base64,A"], lookup_row=None)
        self.assertNotIn("没识别到图片编号", out)
        m_describe.assert_called_once()


class ReviseChannelListTest(unittest.TestCase):
    """改图管道的模板必须自带渠道 id 清单（2026-10-06 19:22 实录的修复）。

    引用图 +「silver 生成」那次跑成了 jank：`_parse_channel` 认不出 silver
    （3bc54e8 起自定义渠道只走 AI 判），整轮落到 `_revise`，而它的模板只写着
    「skill 沿用原渠道，除非用户点名要换」——**一个渠道 id 都没列**，模型无从
    知道 silver 是渠道，于是沿用了账本里的 jank。改图管道不走 `_MASTER_TEMPLATE`，
    那份模板里的 silver/jank 说明它看不到。
    """

    def test_template_lists_the_custom_channels(self):
        t = direct_gen._REVISE_TEMPLATE
        for name in ("silver", "jank"):
            self.assertIn(name, t, "改图模板里没提渠道 %s" % name)

    def test_template_lists_the_fixed_and_tier_channels(self):
        t = direct_gen._REVISE_TEMPLATE
        for name in ("nai", "qwen_image_v1", "image_gen_v1", "krea2", "nffa",
                     "cunny", "miao", "anima_clear", "hd_3_"):
            self.assertIn(name, t, "改图模板里没提渠道 %s" % name)

    def test_template_warns_custom_channels_have_no_i2i(self):
        """silver/jank 没有垫图骨架——模板得告诉模型垫图轮别填它们。

        不写这句的后果：用户说「silver 图生图」，模型填 silver，代码那头
        `_redraw_capable` 判不过就**静默降回 anima_clear**，出图风味全变。
        """
        self.assertIn("文生图专用", direct_gen._REVISE_TEMPLATE)

    def test_template_still_formats(self):
        """加了清单别把 `.format` 的花括号写坏（模板里 JSON 示例是转义过的）。"""
        out = direct_gen._REVISE_TEMPLATE.format(
            escape="", search="", anchor_note="n", last_skill="jank",
            last_prompt="p", lang="l", text="silver 生成")
        self.assertIn("silver", out)


class WorkflowParamsTest(unittest.TestCase):
    """钉死 sd 新参数与 cunny 工作流结构（2026-10-05 用户拍板）。"""

    def test_sd_adopted_mimoi_series_params(self):
        # sd（image_gen_v1）换成截图（妹妹系列）那套：544×960 / cfg3 /
        # euler+karras / hires 开；LoRA 三连原本就有，不动。
        import json
        with open("skills/image_gen_v1/workflow.json", encoding="utf-8") as f:
            wf = json.loads(f.read().replace("__SEED__", "1"))
        bp = wf["53"]["inputs"]
        self.assertEqual(bp["width"], 544)
        self.assertEqual(bp["height"], 960)
        self.assertEqual(bp["cfg"], 3)
        self.assertEqual(bp["scheduler"], "karras")
        self.assertTrue(bp["enable_hires"])
        self.assertEqual((bp["hires_width"], bp["hires_height"]), (1080, 1920))
        # LoRA 三连还在（contrast/saturation/outline，负强度）
        loras = [wf[k]["inputs"] for k in ("101", "102", "103")]
        self.assertTrue(all(x["strength_model"] < 0 for x in loras))

    def test_cunny_workflow_structure(self):
        # cunny = cunnyfuncky 的 API 化：两段 KSampler + 分块超分链，
        # 两个采样器都要有 __SEED__（全局替换后同种子，两段确定成对）。
        import json
        with open("skills/cunny/workflow.json", encoding="utf-8") as f:
            wf = json.load(f)
        types = [n["class_type"] for n in wf.values()]
        for t in ("ImageTile+", "easy hiresFix", "ImageUntile+",
                  "VAEEncodeTiled", "VAEDecodeTiled"):
            self.assertIn(t, types)
        loras = [n["inputs"] for n in wf.values()
                 if n["class_type"] == "LoraLoader"]
        self.assertEqual(len(loras), 3)
        self.assertTrue(all(x["strength_model"] < 0 for x in loras))
        ks = [n["inputs"] for n in wf.values() if n["class_type"] == "KSampler"]
        self.assertEqual(len(ks), 2)
        self.assertTrue(all(k["seed"] == "__SEED__" for k in ks))
        self.assertEqual((ks[0]["steps"], ks[0]["cfg"], ks[0]["denoise"]),
                         (45, 4.97, 1.0))
        self.assertEqual((ks[1]["steps"], ks[1]["denoise"]), (20, 0.35))

    def test_cunny_build_t2i_fills_placeholders(self):
        # 占位符机制对 cunny 生效：提示词进得去、种子替换成裸数字
        import json
        out = gi.build_t2i_workflow("cunny", "1girl, test", 424242)
        self.assertIsNotNone(out)
        text = out if isinstance(out, str) else json.dumps(out)
        self.assertIn("1girl, test", text)
        self.assertNotIn("__SEED__", text)
        self.assertNotIn("__MULTI_PROMPTS__", text)
        wf = out if isinstance(out, dict) else json.loads(out)
        self.assertTrue(all(k["seed"] == 424242 for k in
                            [n["inputs"] for n in wf.values()
                             if n["class_type"] == "KSampler"]))

    def test_miao_workflow_structure(self):
        # miao = miao.json 的 API 化：**没有 KSampler**，采样在自定义节点
        # BatchPromptImageGenerator 里 —— 提示词从它的 multi_prompts 槽进，
        # 上游是 PrimitiveStringMultiline 的 value（**不是** CLIPTextEncode.text）。
        # 画布上那个死节点 EnvString（读 DEEPSEEK_API_KEY）按用户拍板剔掉了。
        import json
        with open("skills/miao/workflow.json", encoding="utf-8") as f:
            raw = f.read()
        wf = json.loads(raw.replace("__SEED__", "1"))
        types = [n["class_type"] for n in wf.values()]
        self.assertNotIn("KSampler", types)          # 这份骨架本来就没有
        self.assertNotIn("EnvString", types)         # 死节点已剔
        self.assertIn("BatchPromptImageGenerator", types)
        self.assertIn("ImageUpscaleWithModel", types)   # 2x 超分

        gen = [n["inputs"] for n in wf.values()
               if n["class_type"] == "BatchPromptImageGenerator"][0]
        self.assertEqual((gen["width"], gen["height"]), (1024, 1536))
        self.assertEqual((gen["steps"], gen["cfg"]), (45, 6))
        self.assertEqual((gen["sampler_name"], gen["scheduler"]),
                         ("dpmpp_2m", "karras"))
        self.assertEqual(gen["seed"], 1)
        # 种子在文件里是**裸占位符**（不是合法 JSON），靠替换才成立
        self.assertIn('"seed": __SEED__', raw)

        src = wf[str(gen["multi_prompts"][0])]
        self.assertEqual(src["class_type"], "PrimitiveStringMultiline")
        self.assertEqual(src["inputs"]["value"], "__MULTI_PROMPTS__")

        ckpt = [n["inputs"]["ckpt_name"] for n in wf.values()
                if n["class_type"] == "CheckpointLoaderSimple"][0]
        self.assertEqual(ckpt, "miaomiaoRealskin_epsV13.safetensors")
        loras = {n["inputs"]["lora_name"]: n["inputs"]["strength_model"]
                 for n in wf.values() if n["class_type"] == "LoraLoader"}
        self.assertEqual(len(loras), 3)
        self.assertEqual(loras["add_contrast_XL.safetensors"], -1)
        self.assertEqual(loras["add_saturation_XL.safetensors"], 0.2)
        self.assertEqual(loras["facial expression style v2.1.safetensors"], 0.8)

    def test_miao_build_t2i_fills_placeholders(self):
        # 占位符机制对 miao 也生效（钉的是「没有 KSampler 的渠道照样能跑」）：
        # 提示词进 PrimitiveStringMultiline.value，种子替换成裸数字。
        import json
        out = gi.build_t2i_workflow("miao", "1girl, test", 424242)
        self.assertIsNotNone(out)
        text = out if isinstance(out, str) else json.dumps(out)
        self.assertIn("1girl, test", text)
        self.assertNotIn("__SEED__", text)
        self.assertNotIn("__MULTI_PROMPTS__", text)
        wf = out if isinstance(out, dict) else json.loads(out)
        gen = [n["inputs"] for n in wf.values()
               if n["class_type"] == "BatchPromptImageGenerator"][0]
        self.assertEqual(gen["seed"], 424242)


class SearchHookTest(unittest.TestCase):
    """前置搜索钩子（`.env DIRECT_SEARCH`）。

    契约：搜索 Agent **只负责查资料**，拿回来的资料塞进转译 prompt；
    查不到 / 炸了 / 关掉了 → 模板里那一格留空，**照老路走，不连累生图**。

    用户对这块的定位说得极死：「唯一任务就是搜索」。所以这里同时钉住
    **传给搜索 Agent 的只有用户这一轮的原话**——不许夹带历史、不许夹带渠道词。

    2026-10-06 用户拍板「全部流程默认先走一遍搜索 Agent」之后，搜索从
    `_translate` 内部提到了 `decide()` 入口（`_prefetch_search`）。所以这里
    分两段钉：① `_prefetch_search` 本身的行为；② `_translate` / `_revise`
    拿到 doc 后有没有正确塞进模板。
    """

    def setUp(self):
        p = mock.patch.object(direct_gen, "SEARCH_ENABLED", True)
        p.start()
        self.addCleanup(p.stop)

    def _prefetch(self, doc="", boom=False, text="画个胡桃", det=""):
        """跑一次 `_prefetch_search`，返回 (返回的资料包, 搜索 mock)。

        ⚠️ 两段都要 mock：
          - `search_agent.search` —— 走 LLM，用例里绝不出网；
          - `search_tags.extract` —— 它会去读**真的** 32.8 万行标签库
            （首次约 0.5 s + 110 MB），而且结果随库变，不能进单测。
        """
        from app import search_agent
        from app.tools.normal import search_tags

        def fake(need, hint=""):
            if boom:
                raise RuntimeError("搜索炸了")
            return doc

        dp = mock.patch.object(
            search_tags, "extract",
            return_value={"doc": det, "hint": "已经查过的词：X",
                          "confirmed": ["X"], "ambiguous": []})
        dp.start()
        self.addCleanup(dp.stop)
        sp = mock.patch.object(search_agent, "search", side_effect=fake)
        m_search = sp.start()
        self.addCleanup(sp.stop)
        return direct_gen._prefetch_search(text), m_search

    def _translate(self, doc="", text="画个胡桃"):
        """跑一次 _translate，返回喂给转译 LLM 的 prompt。"""
        with mock.patch.object(
                direct_gen.llm, "call_llm",
                return_value='{"skill": "anima_clear", "prompt": "hu tao"}') as m_llm:
            direct_gen._translate(text, [], doc=doc)
        return m_llm.call_args[0][0][0]["content"]

    def test_doc_is_injected_into_the_prompt(self):
        doc, m_search = self._prefetch("角色：胡桃 → hu_tao_(genshin_impact)（原神）")
        m_search.assert_called_once()
        prompt = self._translate(doc=doc)
        self.assertIn("【标签库资料】", prompt)
        self.assertIn("hu_tao_(genshin_impact)", prompt)

    def test_empty_doc_leaves_no_section(self):
        self.assertNotIn("【标签库资料】", self._translate(doc=""))

    def test_search_failure_does_not_break_translation(self):
        doc, _ = self._prefetch(boom=True)
        self.assertEqual(doc, "")
        prompt = self._translate(doc=doc)
        self.assertNotIn("【标签库资料】", prompt)
        self.assertIn("【用户】", prompt)      # 模板照常渲染

    def test_only_the_raw_need_is_passed(self):
        """搜索 Agent 不吃历史、不吃渠道词——只拿用户原话。"""
        _, m_search = self._prefetch("资料", text="画个胡桃")
        self.assertEqual(m_search.call_args[0][0], "画个胡桃")

    def test_disabled_flag_skips_search_entirely(self):
        with mock.patch.object(direct_gen, "SEARCH_ENABLED", False):
            from app import search_agent
            with mock.patch.object(search_agent, "search") as m_search:
                out = direct_gen._prefetch_search("画个胡桃")
        m_search.assert_not_called()
        self.assertEqual(out, "")

    def test_empty_text_skips_search(self):
        _, m_search = self._prefetch("资料", text="   ")
        m_search.assert_not_called()

    def test_template_still_renders_without_search(self):
        """老路必须原样能跑：没有 search 槽的调用方不该炸。"""
        out = direct_gen._MASTER_TEMPLATE.format(
            recent="", text="t", chan_hint="", search="")
        self.assertIn("【用户】", out)


class DocPackTest(unittest.TestCase):
    """资料包 = 定点抽取（代码，确定性）+ 搜索 Agent（LLM，补漏）。

    用户 2026-10-06 原话：「我们进行搜索应该是一套工作流，跑一次搜索，然后
    组合成差不多 500~1000 个字的这样一套东西发给这个生图 API，让它去可以
    参考这个写法。」

    这里钉住拼接顺序、hint 透传、和总长度上限。
    """

    def setUp(self):
        p = mock.patch.object(direct_gen, "SEARCH_ENABLED", True)
        p.start()
        self.addCleanup(p.stop)
        from app.tools.normal import search_tags
        from app import search_agent
        self.st = search_tags
        self.sa = search_agent

    def _run(self, det="", agent="", text="画个胡桃"):
        from app.tools.normal import search_tags
        from app import search_agent

        dp = mock.patch.object(
            search_tags, "extract",
            return_value={"doc": det, "hint": "h", "confirmed": [],
                          "ambiguous": []})
        dp.start()
        self.addCleanup(dp.stop)
        sp = mock.patch.object(search_agent, "search",
                               side_effect=lambda need, hint="": agent)
        m = sp.start()
        self.addCleanup(sp.stop)
        return direct_gen._prefetch_search(text), m

    def test_deterministic_part_comes_first(self):
        """定点抽取排前面：它是确定性真值，LLM 那段是补漏。"""
        out, _ = self._run(det="【定点】连衣裙 → dress", agent="【Agent】泳装 → swimsuit")
        self.assertIn("连衣裙", out)
        self.assertIn("swimsuit", out)
        self.assertLess(out.index("连衣裙"), out.index("swimsuit"))

    def test_hint_is_forwarded_to_the_agent(self):
        """成本全靠这条：告诉模型哪些词代码已经查实了，别重复查。"""
        _, m = self._run(det="【定点】x", agent="y")
        self.assertEqual(m.call_args[1].get("hint"), "h")

    def test_agent_still_gets_the_raw_need(self):
        _, m = self._run(agent="y", text="画个银狼在打游戏")
        self.assertEqual(m.call_args[0][0], "画个银狼在打游戏")

    def test_deterministic_part_alone_is_enough(self):
        """搜索 Agent 返回空（比如它判定「无需查库」）时，定点抽取照样交出去。"""
        out, _ = self._run(det="【定点】连衣裙 → dress", agent="")
        self.assertIn("dress", out)

    def test_agent_part_alone_is_enough(self):
        out, _ = self._run(det="", agent="【Agent】泳装 → swimsuit")
        self.assertIn("swimsuit", out)

    def test_both_empty_gives_empty(self):
        out, _ = self._run(det="", agent="")
        self.assertEqual(out, "")

    def test_extract_failure_falls_back_to_the_agent(self):
        from app.tools.normal import search_tags
        from app import search_agent
        with mock.patch.object(search_tags, "extract",
                               side_effect=RuntimeError("库炸了")):
            with mock.patch.object(search_agent, "search",
                                   side_effect=lambda need, hint="": "【Agent】y"):
                out = direct_gen._prefetch_search("画个胡桃")
        self.assertEqual(out, "【Agent】y")

    def test_total_length_is_capped(self):
        out, _ = self._run(det="甲" * 900, agent="乙" * 900)
        self.assertLessEqual(len(out), direct_gen.DOC_MAX_CHARS + 1)

    def test_cap_keeps_the_deterministic_head(self):
        """超长时从尾巴截——定点抽取是真值，不能被截掉。"""
        out, _ = self._run(det="甲" * 900, agent="乙" * 900)
        self.assertTrue(out.startswith("甲"))
        self.assertIn("…", out)


class DecidePrefetchTest(unittest.TestCase):
    """`decide()` 入口统一算一次搜索，并把 doc 传给下游（2026-10-06 拍板）。

    这一条钉的是**「全部流程默认走」到底有没有落到代码上**：搜索不再是
    `_translate` 的私事，而是 decide 算好往下发。
    """

    def test_decide_prefetches_once_and_passes_doc_down(self):
        with mock.patch.object(direct_gen, "_prefetch_search",
                               return_value="资料X") as m_pre:
            with mock.patch.object(direct_gen, "_translate",
                                   return_value={"skill": "anima_clear",
                                                 "prompt": "p"}) as m_tr:
                with mock.patch.object(direct_gen, "_enqueue",
                                       return_value="ok"):
                    with mock.patch.object(direct_gen, "_remember_job"):
                        direct_gen.decide("大大怪 画个胡桃", [], False,
                                          at_me=True)
        m_pre.assert_called_once()
        self.assertEqual(m_tr.call_args[1].get("doc"), "资料X")


class ReviseSearchTest(unittest.TestCase):
    """改图管道 `_revise` 也要吃前置搜索的资料。

    2026-10-06 查真机日志发现的缺口：`_revise` 走 `app.vision.describe()`，
    **根本不经过 `_translate`**，所以群里最常见的「图生图，角色换XX」
    从来没走过搜索（芽衣 / 琪亚娜 / 伊洛玛丽三轮日志里零文本 LLM 调用）。
    """

    def _run_revise(self, **kw):
        """跑一次 _revise，返回它喂给识图模型的 prompt。"""
        seen = {}

        def fake_describe(url, prompt=None):
            seen["prompt"] = prompt or ""
            return '{"skill": "anima_clear", "prompt": "raiden mei, 1girl"}'

        with mock.patch("app.vision.describe", side_effect=fake_describe):
            with mock.patch.object(direct_gen, "_enqueue", return_value="ok"):
                with mock.patch.object(direct_gen, "_remember_job"):
                    direct_gen._revise("角色换芽衣", ["http://x/1.png"], [],
                                       channel="anima_clear", **kw)
        return seen.get("prompt", "")

    def test_revise_injects_doc(self):
        prompt = self._run_revise(doc="角色：芽衣 → raiden_mei（崩坏3）")
        self.assertIn("【标签库资料】", prompt)
        self.assertIn("raiden_mei", prompt)

    def test_revise_without_doc_leaves_no_section(self):
        prompt = self._run_revise()
        self.assertNotIn("【标签库资料】", prompt)


if __name__ == "__main__":
    unittest.main()
