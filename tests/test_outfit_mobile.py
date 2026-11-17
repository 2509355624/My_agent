# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "outfit.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class OutfitSimpleTest(unittest.TestCase):
    def test_no_emoji_modes_or_gradient_buttons(self):
        html = _html()
        self.assertNotIn("👗", html)
        self.assertNotIn("🤸", html)
        self.assertNotIn("linear-gradient(135deg,var(--accent),var(--accent2))", html)

    def test_head_is_short_and_links_home(self):
        html = _html()
        self.assertIn("<h1>换装</h1>", html)
        self.assertIn('<a class="back" href="/">← 回首页</a>', html)
        self.assertIn("两张图，换衣服或姿势。", html)

    def test_mobile_keeps_two_slots_side_by_side(self):
        html = _html()
        self.assertNotIn(".pair{grid-template-columns:1fr;}", html)
        self.assertIn(".pair { gap: 8px; }", html)

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("slotA", "slotB", "imgA", "imgB", "fileA", "fileB",
                            "go", "status", "asyncHint", "result", "resultImg",
                            "dl", "again"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
