# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "migrate.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class MigrateMobileTest(unittest.TestCase):
    def test_uses_product_sans_and_no_purple_glow(self):
        html = _html()
        self.assertNotIn('"SF Mono", "JetBrains Mono"', html)
        self.assertIn('"Segoe UI", "PingFang SC"', html)
        self.assertNotIn("radial-gradient(ellipse 80% 50%", html)

    def test_head_is_short(self):
        html = _html()
        self.assertIn("<h1>迁移</h1>", html)
        self.assertIn('<a class="back" href="/">← 回首页</a>', html)

    def test_mobile_thumb_uses_aspect_ratio(self):
        html = _html()
        self.assertIn(".thumb { height: auto; aspect-ratio: 3 / 4; }", html)

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("src", "dst", "batch", "reload", "state", "grid"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
