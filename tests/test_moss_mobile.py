# -*- coding: utf-8 -*-
"""moss 漫画台的首屏守卫。

2026-11-17：原来整页等宽字体、技术参数 pill、两行编剧说明和 LoRA 槽位
一起占首屏。改成系统无衬线 + 短 pill + 说明折叠 + LoRA 默认收起。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "moss_studio.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class MossStudioSimpleTest(unittest.TestCase):
    def test_uses_product_sans_not_monospace_everywhere(self):
        html = _html()
        self.assertNotIn('"SF Mono", "JetBrains Mono"', html)
        self.assertIn('"Segoe UI", "PingFang SC"', html)

    def test_tech_pill_is_short(self):
        html = _html()
        self.assertIn('<span class="pill">一页一次生成</span>', html)

    def test_long_hint_is_folded(self):
        html = _html()
        self.assertIn('<details class="note">', html)
        self.assertIn("<summary>说明</summary>", html)
        self.assertIn("编剧只写「这一格画什么」", html)

    def test_lora_is_folded_but_still_there(self):
        html = _html()
        self.assertIn('<details class="card adv" id="lora-card">', html)
        self.assertIn('<div id="lora-rows"></div>', html)
        self.assertIn('id="btn-lora-save"', html)

    def test_mobile_stacks_editor_and_canvas(self):
        html = _html()
        start = html.index("@media (max-width: 640px)")
        block = html[start:]
        self.assertIn(".headgrid { grid-template-columns: 1fr; }", block)
        self.assertIn(".panel .trow { grid-template-columns: 1fr; }", block)
        self.assertIn("input, textarea, select { font-size: 16px; }", block)

    def test_original_ids_are_kept(self):
        html = _html()
        for element_id in (
                "brief", "layout", "style", "styleCustom", "pages", "btn-write",
                "spec-card", "spec-title", "spec-style", "cast-list", "issues",
                "btn-save", "btn-draw-all", "lora-card", "lora-rows",
                "btn-lora-save", "pages-wrap", "zoom", "toast", "histsel",
                "jobbar"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
