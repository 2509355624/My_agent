# -*- coding: utf-8 -*-
"""5174 首页顶栏的手机端折叠守卫。

2026-11-16 实测：390px 视口下 17 个功能按钮竖着堆，顶栏占 621px（屏幕 74%），
聊天区几乎没地方。改成「四个常驻 + ⋯ 更多」折叠。守卫锁住两件事：
1. 桌面端行为不变（更多按钮不出场）；
2. 只切 class、不克隆/不搬 DOM——克隆会产生重复 id（#readModeBtn），
   展开后点到的就不是原按钮了。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "web", "index.html")


def _html():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


class IndexMobileHeaderTest(unittest.TestCase):
    def test_four_named_buttons_are_pinned(self):
        html = _html()
        for target in (
                "<button class=\"btn keep-mobile\" "
                "onclick=\"window.open('/admin','_blank')\"",
                "<button class=\"btn keep-mobile\" "
                "onclick=\"window.open('/status','_blank')\"",
                "<button class=\"btn showcase-go keep-mobile\" "
                "onclick=\"openShowcase()\"",
                "<button class=\"btn keep-mobile\" "
                "onclick=\"window.open('/persona','_blank')\""):
            with self.subTest(target=target):
                self.assertEqual(html.count(target), 1, target)

    def test_only_one_extra_keep_mobile_is_the_toggle(self):
        # 4 个常驻 + 1 个「更多」按钮，不多不少（注释里的字样不算）
        buttons = [ln for ln in _html().splitlines()
                   if ln.strip().startswith("<button") and "keep-mobile" in ln]
        self.assertEqual(len(buttons), 5)

    def test_toggle_only_flips_class(self):
        line = [ln for ln in _html().splitlines()
                if ln.strip().startswith("<button") and "more-toggle" in ln
                and "onclick" in ln]
        self.assertEqual(len(line), 1)
        src = line[0]
        self.assertIn("classList.toggle('more-open')", src)
        for forbidden in ("cloneNode", "appendChild", "innerHTML", "removeChild"):
            self.assertNotIn(forbidden, src)

    def test_more_menu_folds_secondary_buttons(self):
        html = _html()
        self.assertIn(".more-menu {", html)
        self.assertIn("display: none;", html)
        self.assertIn(".header-actions.more-open .more-menu { display: grid; }",
                      html)
        self.assertIn('id="moreMenu"', html)

    def test_mobile_header_stacks_in_two_rows(self):
        html = _html()
        block = html[html.index("@media (max-width: 768px)"):]
        self.assertIn("flex-direction: column;", block)
        self.assertIn(".header-actions .more-toggle { display: inline-flex; }",
                      block)

    def test_more_toggle_is_always_available(self):
        html = _html()
        self.assertIn(".header-actions .more-toggle { display: inline-flex; }",
                      html)


class IndexGalleryEntryTest(unittest.TestCase):
    def test_gallery_entry_opens_local_same_origin(self):
        # 本机 / 内网管理端直连 5174 开画廊，不再绕公网隧道 / 5175 网关。
        self.assertIn("window.open('/gallery','_blank')", _html())

    def test_gallery_entry_no_longer_uses_tunnel_helper(self):
        html = _html()
        self.assertNotIn("openFriendPage", html)
        self.assertNotIn("':5175' + path", html)


if __name__ == "__main__":
    unittest.main()
