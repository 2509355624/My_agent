# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "comic_admin.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class ComicAdminSimpleTest(unittest.TestCase):
    def test_no_purple_glow(self):
        self.assertNotIn("radial-gradient(ellipse 80% 50%", _html())

    def test_head_is_short_and_links_home(self):
        html = _html()
        self.assertIn("<h1>漫画额度</h1>", html)
        self.assertIn('<a class="back" href="/">← 回首页</a>', html)
        self.assertIn('href="/comic_works"', html)
        self.assertIn("漫画台管理", html)

    def test_hints_are_one_line(self):
        html = _html()
        self.assertIn("剩 = 还能用；共 = 给过的总次数。", html)
        self.assertIn("填 QQ，点一下就建号并加次数。", html)

    def test_mobile_inputs_are_16px(self):
        self.assertIn("input[type=text], input[type=number], select { font-size: 16px; }",
                      _html())

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("reload", "bar", "toggleDefaults", "defaultsText",
                            "defaultsEdit", "defaultsFields", "saveDefaults",
                            "accCount", "accounts", "newQQ", "newNum", "newKind",
                            "newCustom"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
