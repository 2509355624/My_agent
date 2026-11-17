"""上下文/压缩配置域：预算与物理窗口封顶。"""

import unittest

from app import config
from app.core.config import context


class ContextConfigTest(unittest.TestCase):
    def test_every_exported_name_matches_legacy_config(self):
        for name in context.__all__:
            self.assertTrue(hasattr(config, name), name)
            self.assertEqual(getattr(config, name), getattr(context, name), name)

    def test_budget_and_turn_limits_are_int(self):
        self.assertIsInstance(context.MAX_TURNS, int)
        self.assertIsInstance(context.CONTEXT_BUDGET, int)
        self.assertIsInstance(context.CONTEXT_MAX_TURNS, int)
        self.assertIsInstance(context.MEMORY_TURN_DIGEST_CHARS, int)

    def test_physical_budget_caps_local_window(self):
        self.assertEqual(context.physical_budget("ollama", "m", 30000),
                         min(30000, int(16384 * 0.7)))
        self.assertEqual(context.physical_budget("ollama", "m", 8000), 8000)

    def test_private_window_table_is_not_exported(self):
        self.assertNotIn("_PROVIDER_CONTEXT_WINDOW", context.__all__)
        self.assertNotIn("_CONTEXT_WINDOW_SAFE_RATIO", context.__all__)
        self.assertFalse([name for name in context.__all__ if name.startswith("_")])


if __name__ == "__main__":
    unittest.main()
