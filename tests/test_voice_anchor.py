# -*- coding: utf-8 -*-
"""卡级声口试点：只在日常轮注入，亲密轮不拼。"""

import unittest

from app.features.persona import head_builder, writing_style
from app.features.persona.cards_store import _norm_card


class VoiceAnchorTest(unittest.TestCase):
    def _card(self):
        return {
            "id": "x", "name": "纱雾", "systemPrompt": "你是纱雾。",
            "voiceAnchor": "她说话短，越紧张越结巴。",
            "voiceExample": "你：画完了吗？\n「……还差一点。」",
            "description": "", "personality": "",
        }

    def test_norm_card_keeps_voice_fields(self):
        card = _norm_card(self._card())
        self.assertEqual(card["voiceAnchor"], "她说话短，越紧张越结巴。")
        self.assertEqual(card["voiceExample"], "你：画完了吗？\n「……还差一点。」")

    def test_daily_head_has_voice_but_intimate_head_does_not(self):
        card = self._card()
        daily = head_builder.build(card, "private_1", "张三", "纱雾",
                                   intimate=False, dynamic=False)
        intimate = head_builder.build(card, "private_1", "张三", "纱雾",
                                      intimate=True, dynamic=False)
        self.assertIn("她说话短，越紧张越结巴。", daily)
        self.assertIn("……还差一点。", daily)
        self.assertNotIn("她说话短，越紧张越结巴。", intimate)
        self.assertNotIn("……还差一点。", intimate)


if __name__ == "__main__":
    unittest.main()
