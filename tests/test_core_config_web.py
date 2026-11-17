"""Web 配置域：端口、管理访问范围、画廊根目录、工坊入口。"""

import unittest

from app import config
from app.core.config import web


class WebConfigTest(unittest.TestCase):
    def test_old_and_new_paths_expose_the_same_values(self):
        for name in (
            "AGENT_PORT",
            "ADMIN_ALLOW_REMOTE",
            "ADMIN_ALLOW_REMOTE_LAN",
            "GALLERY_ROOTS",
            "GALLERY_OWNER_QQ",
            "WORKSHOP_HTML",
        ):
            self.assertTrue(hasattr(web, name), name)
            self.assertEqual(getattr(config, name), getattr(web, name), name)

    def test_gallery_roots_parser_keeps_paths_and_names(self):
        self.assertEqual(
            web._parse_gallery_roots(r"A|C:\pics;D:\other"),
            (("A", r"C:\pics"), ("other", r"D:\other")),
        )

    def test_private_values_are_not_exported(self):
        self.assertNotIn("_ADMIN_REMOTE_RAW", web.__all__)
        self.assertNotIn("_parse_gallery_roots", web.__all__)


if __name__ == "__main__":
    unittest.main()
