"""看门狗配置域：静默判据和 ComfyUI 总闸独立于 QQ 接入。"""

import unittest

from app import config
from app.core.config import qq, watchdog


class WatchdogConfigTest(unittest.TestCase):
    def test_every_exported_name_matches_legacy_config(self):
        for name in watchdog.__all__:
            self.assertTrue(hasattr(config, name), name)
            self.assertEqual(getattr(config, name), getattr(watchdog, name), name)

    def test_types_are_stable(self):
        self.assertIsInstance(watchdog.WATCHDOG_SILENCE_SECONDS, float)
        self.assertIsInstance(watchdog.WATCHDOG_SILENCE_COOLDOWN, float)
        self.assertIsInstance(watchdog.WATCHDOG_SILENCE_MAX_GAP, float)
        self.assertIsInstance(watchdog.WATCHDOG_SILENCE_RESTART, bool)
        self.assertIsInstance(watchdog.WATCHDOG_COMFY_ENABLED, bool)

    def test_watchdog_is_not_mixed_into_qq(self):
        self.assertFalse(hasattr(qq, "WATCHDOG_SILENCE_SECONDS"))
        self.assertNotIn("os", watchdog.__all__)
        self.assertFalse([name for name in watchdog.__all__ if name.startswith("_")])


if __name__ == "__main__":
    unittest.main()
