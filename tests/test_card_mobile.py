# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "card_studio.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class CardStudioMobileTest(unittest.TestCase):
    def test_uses_product_sans(self):
        html = _html()
        self.assertNotIn('"SF Mono", "JetBrains Mono"', html)
        self.assertIn('"Segoe UI", "PingFang SC"', html)

    def test_topbar_is_short(self):
        html = _html()
        self.assertIn("<h1>造卡</h1>", html)
        self.assertIn('<a class="back" href="/">← 回首页</a>', html)
        self.assertNotIn("星陨AI 的六个模块", html)

    def test_mobile_cover_is_capped(self):
        html = _html()
        start = html.index("@media (max-width: 860px)")
        block = html[start:]
        self.assertIn(".coverbox { max-width: 220px;", block)
        self.assertIn(".field input { width: 100% !important; }", block)

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("brief", "btnDraft", "btnAgain", "result", "coverbox",
                            "coverEmpty", "btnCover", "coverState", "fName",
                            "fGender", "fCats", "fTags", "fields", "warns",
                            "btnSave", "btnCopyAll", "btnExport", "msg", "msg2"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
