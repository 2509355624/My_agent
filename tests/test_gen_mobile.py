# -*- coding: utf-8 -*-
"""跑图台的手机端守卫。

2026-11-17：桌面是三栏（渠道 208 + 画布 + 历史 264），手机上必然挤爆。
改成单列：渠道横向条、画布在上、历史沉底；同时去掉整页等宽字。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "gen_pad.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class GenPadMobileTest(unittest.TestCase):
    def test_uses_product_sans_not_monospace_everywhere(self):
        html = _html()
        self.assertNotIn('"SF Mono", "JetBrains Mono"', html)
        self.assertIn('"Segoe UI", "PingFang SC"', html)

    def test_topbar_is_short_and_has_a_way_back(self):
        html = _html()
        self.assertIn("<h1>跑图台</h1>", html)
        self.assertIn("选渠道 · 写提示词 · 出图", html)
        self.assertIn('<a class="link" href="/">← 回首页</a>', html)

    def test_mobile_is_single_column_with_horizontal_channels(self):
        html = _html()
        start = html.index("@media (max-width: 760px)")
        block = html[start:]
        self.assertIn("flex-direction: column;", block)
        self.assertIn(".ch-list { display: flex; gap: 8px; overflow-x: auto;", block)
        self.assertIn(".history { width: 100%;", block)

    def test_original_ids_are_kept(self):
        html = _html()
        for element_id in (
                "chList", "view", "viewEmpty", "jobs", "shot", "prompt", "neg",
                "curChan", "paramsOn", "landscapeOn", "stepsIn", "cfgIn",
                "wIn", "hIn", "sizePreset", "btnGen", "btnShow", "history",
                "loraRows", "loraToggle", "naiOptions"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
