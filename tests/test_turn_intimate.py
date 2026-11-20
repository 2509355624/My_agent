# -*- coding: utf-8 -*-
"""亲密写法只在真正判成亲密时拼；日常 neutral 不拖那 1,900 字。"""

import unittest

from app.features.persona import turn


class IntimateIntentTest(unittest.TestCase):
    def test_only_intimate_and_unknown_get_the_guide(self):
        for intent in (None, "", "intimate"):
            with self.subTest(intent=intent):
                self.assertTrue(turn.intimate_for_intent(intent))
        for intent in ("neutral", "joke", "literal", "action"):
            with self.subTest(intent=intent):
                self.assertFalse(turn.intimate_for_intent(intent))


if __name__ == "__main__":
    unittest.main()
