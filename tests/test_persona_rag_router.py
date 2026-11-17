"""角色扮演 RAG 路由（2026-11-18）。

P0 只守三件事：
  · 编剧三个入口的输出契约里都有 rag；
  · judge 解析/透传 rag，缺字段时保留 None；
  · turn 的兜底规则不会把显式过去问法漏掉，也不会把 detail 降级。
"""
import json
import unittest
from unittest import mock

from app.features.persona import judge as J
from app.features.persona import judge_fields as F
from app.features.persona import service as persona
from app.features.persona.prompts import _INTENT_PROMPT, _SCORE_PROMPT, _SCORE_PROMPT_V2


class RagValueTest(unittest.TestCase):
    def test_norm_rag_only_accepts_three_values(self):
        self.assertEqual(F._norm_rag("none"), "none")
        self.assertEqual(F._norm_rag("summary"), "summary")
        self.assertEqual(F._norm_rag("detail"), "detail")
        self.assertIsNone(F._norm_rag(""))
        self.assertIsNone(F._norm_rag("true"))
        self.assertIsNone(F._norm_rag(None))

    def test_fallback_recognizes_explicit_past(self):
        self.assertIsNone(F.rag_fallback("继续，抱紧我"))
        self.assertEqual(F.rag_fallback("上次我们干嘛了"), "summary")
        self.assertEqual(F.rag_fallback("第一次是在哪"), "detail")
        self.assertEqual(F.rag_fallback("你当时原话怎么说的"), "detail")

    def test_resolve_rag_lets_explicit_past_upgrade(self):
        self.assertEqual(F.resolve_rag("none", "继续"), "none")
        self.assertEqual(F.resolve_rag("none", "上次我们干嘛了"), "summary")
        self.assertEqual(F.resolve_rag("summary", "第一次在哪"), "detail")
        self.assertEqual(F.resolve_rag(None, "今天天气不错"), "none")

    def test_resolve_rag_never_downgrades_detail(self):
        self.assertEqual(F.resolve_rag("detail", "继续"), "detail")
        self.assertEqual(F.resolve_rag("detail", "今天天气不错"), "detail")

    def test_resolve_rag_trusts_judge_when_fallback_empty(self):
        self.assertEqual(F.resolve_rag("summary", "继续"), "summary")
        self.assertEqual(F.resolve_rag("none", "继续"), "none")


class RagPromptContractTest(unittest.TestCase):
    def test_all_three_prompts_ask_for_rag(self):
        for prompt in (_INTENT_PROMPT, _SCORE_PROMPT, _SCORE_PROMPT_V2):
            self.assertIn('"rag": "none | summary | detail"', prompt)
            self.assertIn("翻旧账", prompt)


class RagJudgeParseTest(unittest.TestCase):
    @staticmethod
    def _llm(payload):
        def call(_messages, **_kwargs):
            return json.dumps(payload, ensure_ascii=False)
        return call

    def test_free_mode_passes_rag_through(self):
        payload = {
            "intent": "neutral", "rag": "none", "finish": "none",
            "note": "随口回一句", "addressed": True, "secret": False,
            "facts": [], "items": {},
        }
        _intent, _note, extra = J.judge_intent(
            {}, "继续", llm_call=self._llm(payload),
            name_fn=lambda _card, default: "她", recent_fn=lambda _h: "")
        self.assertEqual(extra["rag"], "none")

    def test_free_mode_invalid_intent_still_keeps_rag(self):
        payload = {"intent": "unknown", "rag": "detail", "facts": []}
        _intent, _note, extra = J.judge_intent(
            {}, "第一次是在哪", llm_call=self._llm(payload),
            name_fn=lambda _card, default: "她", recent_fn=lambda _h: "")
        self.assertEqual(extra["rag"], "detail")

    def _ctx_v1(self):
        return {
            "value": 0, "tier": "正常关系", "level": "隔得很远",
            "arousal": 0, "arousalName": "—", "excite": 0,
            "exciteName": "还没进场景", "dynamicBrief": "（无）",
            "corrupt": 0, "submit": 0, "suspicion": 0,
            "devText": "（都没开发过）", "recent": "（无）",
        }

    def test_v1_score_passes_rag_through(self):
        payload = {"intent": "neutral", "rag": "summary", "delta": 0,
                   "turn_type": "chat", "reason": "", "note": "",
                   "facts": [], "addressed": True, "secret": False,
                   "finish": "none", "time": "none", "phase": "", "plan": ""}
        with mock.patch.object(J.affection, "scoring_ctx",
                               return_value=self._ctx_v1()):
            _intent, _note, score = J.judge_intent_scored(
                {}, "上次我们干嘛了", llm_call=self._llm(payload),
                profile_fn=lambda *a, **k: "角色", recent_fn=lambda _h: "")
        self.assertEqual(score["rag"], "summary")

    def test_v2_score_passes_rag_through(self):
        payload = {"intent": "neutral", "rag": "detail", "delta": 0,
                   "turn_type": "chat", "reason": "", "note": "",
                   "facts": [], "addressed": True, "secret": False,
                   "finish": "none", "time": "none", "phase": "", "plan": "",
                   "place": ""}
        with mock.patch.object(J.affection, "scoring_ctx", return_value={}), \
             mock.patch.object(persona, "_score_profile", return_value="角色"), \
             mock.patch.object(persona, "_llm_call", self._llm(payload)):
            _intent, _note, score = J.judge_v2(
                {}, "第一次是在哪", line_key="test")
        self.assertEqual(score["rag"], "detail")


if __name__ == "__main__":
    unittest.main()
