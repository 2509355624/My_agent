# -*- coding: utf-8 -*-
"""5174 /admin 管理页的手机端守卫。

2026-11-16 实测 390px：顶栏 5 个链接被 .spacer 挤成窄列，"漫画台管理 →"
逐字竖排；裸表格 425px + 会话列含 <b> 的长提示把文档撑到 618px。
修完实测 scrollWidth 390 = 视口，顶栏链接高 22px（单行）。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = os.path.join(ROOT, "web", "admin.html")


def _html():
    with open(ADMIN, encoding="utf-8") as f:
        return f.read()


class AdminMobileTest(unittest.TestCase):
    def setUp(self):
        html = _html()
        self.block = html[html.index("@media (max-width: 640px)"):
                          html.index("</style>", html.index("@media (max-width: 640px)"))]

    def test_viewport_allows_pinching_but_not_auto_zoom(self):
        html = _html()
        self.assertIn("width=device-width, initial-scale=1.0, maximum-scale=5.0",
                      html)

    def test_font_scaling_is_locked(self):
        self.assertIn("-webkit-text-size-adjust: 100%", self.block)

    def test_header_links_do_not_stack_vertically(self):
        self.assertIn("header { flex-wrap: wrap;", self.block)
        self.assertIn("header .spacer { display: none; }", self.block)
        self.assertIn("header a.back, header button.ghost {", self.block)
        self.assertIn("white-space: nowrap;", self.block)

    def test_tables_scroll_inside_themselves(self):
        self.assertIn("display: block; width: 100%;", self.block)
        self.assertIn("overflow-x: auto; -webkit-overflow-scrolling: touch;",
                      self.block)

    def test_long_hints_are_allowed_to_shrink(self):
        self.assertIn(".sess-foot > * { min-width: 0; max-width: 100%; }",
                      self.block)
        self.assertIn(".sess-foot .hint { white-space: normal; "
                      "overflow-wrap: anywhere; }", self.block)

    def test_desktop_rules_are_untouched(self):
        html = _html()
        before = html[:html.index("@media (max-width: 640px)")]
        self.assertIn("width: 200px; flex: 0 0 200px;", before)
        self.assertIn("width: 320px; flex: 0 0 320px;", before)


if __name__ == "__main__":
    unittest.main()
