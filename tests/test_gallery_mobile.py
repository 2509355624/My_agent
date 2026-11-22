# -*- coding: utf-8 -*-
"""画廊页的手机端顶栏守卫。

2026-11-17：顶栏原来同时放「站长/公开/我的」三个 tab 和预设、工坊、
角色扮演、漫画四个入口，320/360px 上挤成一排短标签。改成：
标题 + 三个 tab + 一个「更多」，四个入口进菜单。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GALLERY = os.path.join(ROOT, "web", "gallery.html")


def _html():
    with open(GALLERY, encoding="utf-8") as f:
        return f.read()


class GalleryHeaderTest(unittest.TestCase):
    def test_header_only_keeps_three_tabs_and_one_more_button(self):
        html = _html()
        start = html.index("<header")
        end = html.index("</header>")
        block = html[start:end]
        self.assertEqual(block.count("<button"), 4)  # 3 个 tab + 更多
        for tab in ('data-tab="owner"', 'data-tab="pub"', 'id="tabMine"'):
            self.assertIn(tab, block)

    def test_secondary_entries_live_in_the_more_menu(self):
        html = _html()
        start = html.index("<header")
        end = html.index("</header>")
        block = html[start:end]
        self.assertIn('id="moreMenu"', block)
        for href in ("/preset", "/workshop", "/persona", "/comic"):
            self.assertIn('href="%s"' % href, block)
        self.assertIn('id="presetLink"', block)
        self.assertIn('id="guestLogin"', block)

    def test_more_menu_is_plain_and_real(self):
        html = _html()
        self.assertIn(".more-menu{display:none;", html)
        self.assertIn("header.more-open .more-menu{display:grid;", html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('btn.setAttribute("aria-expanded"', html)
        self.assertIn('e.key === "Escape"', html)
        # 顶栏不再用毛玻璃；详情/查看器的浮层不在这条守卫里。
        start = html.index("<header")
        end = html.index("</header>")
        self.assertNotIn("backdrop-filter", html[start:end])

    def test_owner_toolbar_puts_low_frequency_actions_behind_more(self):
        html = _html()
        self.assertIn('more.id = "toolMore"', html)
        self.assertIn('btn("＋ 扫描文件夹", "primary", openScan)', html)
        # 「全部重扫 / 清理失效」只能出现在 toolMore 里，不能和主操作并排。
        tools = html[html.index("function renderTools()"):]
        tools = tools[:tools.index("function btn(")]
        self.assertIn('more.appendChild(btn("↻ 全部重扫"', tools)
        self.assertIn('more.appendChild(btn("⌫ 清理失效"', tools)
        self.assertIn('more.appendChild(btn("🗑 清空扫描结果"', tools)
        self.assertNotIn('t.appendChild(btn("↻ 全部重扫"', tools)
        self.assertNotIn('t.appendChild(btn("⌫ 清理失效"', tools)

    def test_scan_dialog_blocks_drive_root(self):
        """整块盘不能扫：否则 D:\\AI、D:\\Users 会全变成作品集。"""
        html = _html()
        self.assertIn("function isDriveRoot(p)", html)
        self.assertIn('b.textContent = blocked ? "请进到文件夹" : "扫描这里"', html)
        self.assertIn("不能扫描整块盘，请进到具体文件夹", html)

    def test_scan_dialog_has_native_folder_picker(self):
        html = _html()
        self.assertIn('id="scpick"', html)
        self.assertIn("从电脑中选择文件夹", html)
        self.assertIn("api(\"pick-folder?initial=", html)
        self.assertIn('$("scpick").onclick = pickFolder;', html)

    def test_category_bar_shows_on_phone_without_owner_flag(self):
        """手机走隧道时后端 owner=false，分类条不能因此被藏起来。

        分类只是文件夹名、作品本来就公开列出；真正的隐私在隐藏墙，
        那里另有 owner 判定，不受这条影响。
        """
        html = _html()
        self.assertIn('var show = S.tab === "owner" && !S.hiddenView', html)
        # 显示条件里不能再有 S.owner，否则手机端永远看不到分类条
        cond = html.split("var show = S.tab === \"owner\"", 1)[1].split(";", 1)[0]
        self.assertNotIn("S.owner", cond)

    def test_original_ids_are_kept(self):
        html = _html()
        for element_id in (
                "galHeader", "moreBtn", "moreMenu", "presetLink", "guestLogin",
                "tabMine", "subtabs", "tools", "grps", "hint", "grid",
                "detail", "dback", "dgrid", "viewer", "stage", "track",
                "up", "scanbox", "toast"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
