# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "comic_works.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class ComicWorksMobileTest(unittest.TestCase):
    def test_no_purple_glow(self):
        self.assertNotIn("radial-gradient(ellipse 80% 50%", _html())

    def test_head_is_short_and_links_home(self):
        html = _html()
        self.assertIn("<h1>作品</h1>", html)
        self.assertIn('<a class="back" href="/">← 回首页</a>', html)
        self.assertIn("群友作品浏览", html)

    def test_mobile_accounts_scroll_sideways(self):
        html = _html()
        self.assertIn("display: flex; flex-wrap: nowrap;", html)
        self.assertIn("overflow-x: auto;", html)
        self.assertIn("minmax(140px, 1fr)", html)

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("btnReload", "bar", "wall", "accounts"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
