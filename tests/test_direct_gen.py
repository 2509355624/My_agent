#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直达生图管道（app/direct_gen.py）测试。

核心契约：
- /菜单、裸 @ → 菜单常量，零 LLM
- 生图意图 → 一次转译调用 → 校验 JSON → 直接入队（跳过确认卡）
- 闲聊 / 主动接话 / 转译失败 / 非画图请求 → None 放行 agent
"""
import unittest
from unittest import mock

from app import direct_gen, image_jobs
from app.tools.normal import generate_image as gi

RECEIPT = image_jobs.RECEIPT_SENT_MARK + "回执"


class MenuAndGateTest(unittest.TestCase):
    def test_menu_keywords_return_constant(self):
        for t in ("菜单", "/菜单", "！菜单", "help", "帮助", "指令",
                  ""):                      # 裸 @（剥完前缀啥都不剩）也回菜单
            with self.subTest(t=t):
                self.assertEqual(direct_gen.decide(t, [], False),
                                 direct_gen.MENU_TEXT)

    def test_attribution_prefix_is_stripped_before_menu_match(self):
        self.assertEqual(
            direct_gen.decide("胡桃桃：菜单", [], False),
            direct_gen.MENU_TEXT)

    def test_voluntary_turns_never_taken_over(self):
        # 主动接话轮（没人 @ 它）就算带着画图动词也不进管道
        self.assertIsNone(direct_gen.decide("画一只猫", [], True))

    def test_non_image_text_passes_through(self):
        self.assertIsNone(direct_gen.decide("今天天气不错", [], False))
        self.assertIsNone(direct_gen.decide("生成一下总结", [], False))


class DirectEnqueueTest(unittest.TestCase):
    def _decide(self, llm_reply, text="画一只戴帽子的橘猫", history=None):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value=llm_reply) as m_llm, \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            out = direct_gen.decide(text, history or [], False)
        return out, m_llm, m_gen

    def test_translated_json_enqueues_directly(self):
        out, m_llm, m_gen = self._decide(
            '{"skill": "hd_2_gloss", "prompt": "1girl, hat"}')
        # 入队成功且回执已直发 → 本轮闭嘴
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, hat", skill="hd_2_gloss",
                                      _skip_confirm=True)
        # 转译调用只发一次、不带 agent 的系统头
        self.assertEqual(m_llm.call_count, 1)
        self.assertEqual(m_llm.call_args[0][0][0]["role"], "user")

    def test_recent_history_is_passed_for_coreference(self):
        hist = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "那只猫真可爱"},
                {"role": "assistant", "content": "是呀"}]
        out, m_llm, _ = self._decide(
            '{"skill": "anima_clear", "prompt": "cat"}',
            text="把它画出来", history=hist)
        self.assertEqual(out, "")
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("那只猫真可爱", sent)
        self.assertIn("把它画出来", sent)

    def test_unknown_skill_falls_back_to_default(self):
        out, _, m_gen = self._decide(
            '{"skill": "gpt4o-image", "prompt": "cat"}')
        self.assertEqual(out, "")
        self.assertEqual(m_gen.call_args.kwargs["skill"],
                         direct_gen._DEFAULT_SKILL)

    def test_non_image_verdict_passes_through(self):
        out, m_llm, m_gen = self._decide('{"skill": "", "prompt": ""}')
        self.assertIsNone(out)
        m_gen.assert_not_called()

    def test_broken_json_passes_through(self):
        for bad in ("我不是 JSON", '{"skill": "anima_clear"',
                    '前置废话 {"skill": ok}'):
            with self.subTest(bad=bad):
                out, m_llm, m_gen = self._decide(bad)
                self.assertIsNone(out)
                m_gen.assert_not_called()

    def test_llm_failure_passes_through(self):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               side_effect=RuntimeError("429")), \
             mock.patch.object(gi, "_generate_image") as m_gen:
            out = direct_gen.decide("画一只猫", [], False)
        self.assertIsNone(out)
        m_gen.assert_not_called()

    def test_tool_error_is_humanized(self):
        # 工具的错误文案带「直接告诉对方…」这类模型指示，直达管道只发第一句
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"skill": "nai", "prompt": "x"}'), \
             mock.patch.object(gi, "_generate_image",
                               return_value="错误：NAI 仅支持 QQ。"
                                            "直接告诉对方现在用不了。"):
            out = direct_gen.decide("nai 画一只猫", [], False)
        self.assertEqual(out, "错误：NAI 仅支持 QQ。")

    def test_tool_other_text_is_delivered_verbatim(self):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               return_value='{"skill": "nai", "prompt": "x"}'), \
             mock.patch.object(gi, "_generate_image", return_value="排队中"):
            out = direct_gen.decide("nai 画一只猫", [], False)
        self.assertEqual(out, "排队中")


class SkipConfirmTest(unittest.TestCase):
    def test_intercept_skips_when_told(self):
        # 直达管道标了 skip_confirm：就算在 QQ 轮上下文里也不拦
        with mock.patch.object(direct_gen, "image_jobs") as _:
            pass  # 占位：direct_gen import 的 image_jobs 不影响本用例
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
