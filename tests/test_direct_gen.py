#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直达生图管道（app/direct_gen.py）测试。

核心契约（2026-10-05 agent 退场版）：
- /菜单、裸 @、任何非工具 @ 轮 → 菜单常量，零 LLM，绝不放行 agent
- 生图意图 → 一次转译调用 → 校验 JSON → 直接入队（跳过确认卡）
- 引用图 + 意见 → 识图 + 会话上次任务 → 修正调用 → 重新入队
- 转译失败 → 错误提示 + 菜单，不进 agent
- 只有 ENABLED=False / 主动接话轮才返回 None
"""
import unittest
from unittest import mock

from app import direct_gen, image_jobs
from app.direct_gen import MENU_TEXT
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

    def test_non_image_text_returns_menu_not_agent(self):
        # agent 退场：@ 了但不是生图指令 → 菜单（绝不进 agent 循环）
        for t in ("今天天气不错", "生成一下总结", "你好"):
            with self.subTest(t=t):
                self.assertEqual(direct_gen.decide(t, [], False),
                                 direct_gen.MENU_TEXT)


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

    def test_non_image_verdict_returns_menu(self):
        # 模型判「跟画图无关」→ 回菜单让人照格式来，不进 agent
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

    def test_llm_failure_returns_hint_not_agent(self):
        with mock.patch.object(direct_gen.llm, "call_llm",
                               side_effect=RuntimeError("429")), \
             mock.patch.object(gi, "_generate_image") as m_gen:
            out = direct_gen.decide("画一只猫", [], False)
        self.assertIn("渠道", out)          # 错误提示带格式引导
        self.assertIn(MENU_TEXT, out)
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


class RevisionPipelineTest(unittest.TestCase):
    """改图管道：引用图 + 意见 → 识图 + 上次任务 → 修正调用 → 重新入队。"""

    def _decide(self, text, llm_reply, seen="一个女孩，六根手指", last_job=None):
        with mock.patch.object(direct_gen, "llm") as m_llm_mod, \
             mock.patch.object(direct_gen.qq_api, "current_session_key",
                               return_value="group_1"), \
             mock.patch("app.vision.describe", return_value=seen), \
             mock.patch.object(gi, "_generate_image",
                               return_value=RECEIPT) as m_gen:
            m_llm_mod.call_llm.return_value = llm_reply
            if last_job:
                direct_gen._remember_job("group_1", last_job["skill"],
                                         last_job["prompt"])
            out = direct_gen.decide("多手多脚了", [{"role": "user",
                                                    "content": "画个女孩"}],
                                    False, data_urls=["data:image/jpeg;base64,A"])
            direct_gen._LAST_JOB.pop("group_1", None)
        return out, m_llm_mod.call_llm, m_gen

    def test_revision_uses_vision_and_last_prompt(self):
        out, m_llm, m_gen = self._decide(
            "多手多脚了",
            '{"skill": "anima_clear", "prompt": "1girl, five fingers"}',
            last_job={"skill": "anima_clear", "prompt": "1girl, old"})
        self.assertEqual(out, "")
        m_gen.assert_called_once_with("1girl, five fingers",
                                      skill="anima_clear", _skip_confirm=True)
        sent = m_llm.call_args[0][0][0]["content"]
        self.assertIn("六根手指", sent)      # 识图描述进来了
        self.assertIn("1girl, old", sent)    # 原提示词进来了
        self.assertIn("多手多脚了", sent)    # 用户意见进来了

    def test_revision_without_any_context_guides_user(self):
        out, m_llm, m_gen = self._decide("多手多脚了", "x", seen="")
        self.assertIn("没认出引用的图", out)
        m_gen.assert_not_called()

    def test_revision_fails_translating_returns_error(self):
        out, _, m_gen = self._decide(
            "多手多脚了", "不是 JSON",
            last_job={"skill": "anima_clear", "prompt": "1girl"})
        self.assertIn("没解析出来", out)
        m_gen.assert_not_called()


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
