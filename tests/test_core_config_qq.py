"""QQ 接入与通知配置域：全部导出仍可从旧 app.config 取到。"""

import unittest

from app import config
from app.core.config import qq


class QQConfigTest(unittest.TestCase):
    def test_every_exported_name_matches_legacy_config(self):
        for name in qq.__all__:
            self.assertTrue(hasattr(config, name), name)
            self.assertEqual(getattr(config, name), getattr(qq, name), name)

    def test_webui_port_and_notify_path_are_in_qq_domain(self):
        self.assertIsInstance(qq.WEBUI_PORT, int)
        self.assertIsInstance(qq.NOTIFY_QRCODE_PATH, str)
        self.assertEqual(qq.WEBUI_PORT, config.WEBUI_PORT)
        self.assertEqual(qq.NOTIFY_QRCODE_PATH, config.NOTIFY_QRCODE_PATH)

    def test_public_names_are_exported(self):
        self.assertIn("QQ_TOKEN", qq.__all__)
        self.assertIn("WEBUI_PORT", qq.__all__)
        self.assertNotIn("os", qq.__all__)
        self.assertFalse([name for name in qq.__all__ if name.startswith("_")])


if __name__ == "__main__":
    unittest.main()
