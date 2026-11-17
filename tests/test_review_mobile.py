# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "review.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class ReviewMobileTest(unittest.TestCase):
    def test_uses_product_sans_and_no_purple_glow(self):
        html = _html()
        self.assertNotIn('"SF Mono", "JetBrains Mono"', html)
        self.assertIn('"Segoe UI", "PingFang SC"', html)
        self.assertNotIn("radial-gradient(ellipse 80% 50%", html)

    def test_head_is_short_and_links_home(self):
        html = _html()
        self.assertIn("<h1>审核</h1>", html)
        self.assertIn('<a class="back" href="/">← 回首页</a>', html)

    def test_mobile_cards_stack(self):
        html = _html()
        self.assertIn(".card { flex-direction: column; gap: 10px; }", html)
        self.assertIn(".thumb { width: 100%; max-width: 280px; }", html)

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("badge", "refresh", "onlyPending", "auto", "state",
                            "list"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
