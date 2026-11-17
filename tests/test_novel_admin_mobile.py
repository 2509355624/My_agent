# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "novel_admin.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class NovelAdminMobileTest(unittest.TestCase):
    def test_mobile_stacks_side_and_body(self):
        html = _html()
        self.assertIn("@media (max-width: 760px)", html)
        self.assertIn(".app { display: flex; flex-direction: column;", html)
        self.assertIn(".side { height: auto; max-height: 48vh;", html)

    def test_search_is_short_and_has_home_link(self):
        html = _html()
        self.assertIn('placeholder="搜名字 / QQ"', html)
        self.assertIn('<a href="/">← 回首页</a>', html)

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("cnt", "q", "reload", "list", "title", "meta", "body"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
