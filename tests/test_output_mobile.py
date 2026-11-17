# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "output_manager.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class OutputManagerMobileTest(unittest.TestCase):
    def test_uses_product_sans(self):
        html = _html()
        self.assertNotIn('"SF Mono", "JetBrains Mono"', html)
        self.assertIn('"Segoe UI", "PingFang SC"', html)

    def test_topbar_is_short_and_links_home(self):
        self.assertIn("<h1>图片</h1>", _html())
        self.assertIn('<a class="btn small" href="/">← 回首页</a>', _html())

    def test_touch_devices_can_see_the_pick_checkbox(self):
        html = _html()
        self.assertIn("@media (hover: none)", html)
        self.assertIn(".cell .pick { display: block; }", html)

    def test_mobile_tree_is_capped(self):
        html = _html()
        start = html.index("@media (max-width: 760px)")
        block = html[start:]
        self.assertIn("#tree { max-height: 180px;", block)

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("tree", "main", "crumb", "toolbar", "dirInfo",
                            "grid", "stat", "btnRefresh", "lb", "mask", "dlg",
                            "toast"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
