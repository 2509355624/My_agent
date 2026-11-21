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

    def test_tables_scroll_inside_their_own_box(self):
        """列多的表格（用量 6 列 / 账单 8 列）在容器里横滑，不把整页撑宽。

        2026-11-21 需求变更：原来在 640 断点给 table 设 display:block 当滚动
        容器，实测表格的 min-content 仍然把外层 .card 一起撑宽（8 列表格：
        文档 584px / 视口 477px，整页往右延伸）。改成 #usageTable / #billTable
        这两个容器自己横滑，表格保持 table 布局并按 max-content 定列宽。
        """
        html = _html()
        self.assertIn("#usageTable, #billTable {", html)
        self.assertIn("overflow-x: auto; max-width: 100%;", html)
        self.assertIn("#usageTable table, #billTable table { min-width: max-content; }",
                      html)
        # 表格不再自己当滚动容器，避免容器与表格各套一层
        self.assertNotIn("display: block; width: 100%;", html)

    def test_stacked_cards_fill_the_viewport(self):
        """窄屏卡片必须占满整行：flex 列布局不能再沿用 grid 的 align-items:start。

        2026-11-21 用户截图：人设卡只占半屏。因为切成 flex 列后
        align-items:start 等于「按内容宽度排」，卡片宽度被里面的 textarea
        默认列宽定住（实测 390px 下卡片 202px / 视口 477px）。
        """
        html = _html()
        i = html.index("@media (max-width: 1180px)")
        block = html[i:html.index("@media (max-width: 640px)", i)]
        self.assertIn("align-items: stretch;", block)
        self.assertIn(".panel > * { width: 100%; max-width: 100%; min-width: 0; }",
                      block)

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
