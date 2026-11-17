# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "compat.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class CompatMobileTest(unittest.TestCase):
    def test_long_tables_are_collapsed(self):
        html = _html()
        for table_id in ("tView", "tCss", "tApi", "tEnv"):
            self.assertIn('<details class="sec">', html)
            self.assertIn('id="%s"' % table_id, html)
        self.assertEqual(html.count("<details class=\"sec\">"), 4)

    def test_head_is_short_and_links_home(self):
        html = _html()
        self.assertIn("<h1>自检</h1>", html)
        self.assertIn('<a href="/">← 回首页</a>', html)
        self.assertIn("先看结论", html)

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("tView", "hView", "tCss", "tApi", "tEnv", "verdict",
                            "vBig", "vWhy", "copy", "reload", "toast", "dump"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
