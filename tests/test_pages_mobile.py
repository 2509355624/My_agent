# -*- coding: utf-8 -*-
"""其余 20 个页面的手机端基线守卫（2026-11-16）。

统一层：所有页面锁 text-size-adjust，防国产浏览器自动放大字号。
针对性修复：status 表格横滑、comic_admin 按钮不竖排、characters 顶栏说明
限两行、moss_studio 收起长 pill、gen_pad/compat 输入框 ≥16px 防聚焦缩放。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

PAGES = (
    "characters compat gen_pad novel_admin persona_admin review "
    "card_studio comic_admin comic_studio comic_works friend guest_view "
    "migrate moss_studio output_manager runbench status outfit "
    "comic_studio_friend novel"
).split()


def _html(name):
    with open(os.path.join(WEB, name + ".html"), encoding="utf-8") as f:
        return f.read()


class PagesMobileBaselineTest(unittest.TestCase):
    def test_every_page_locks_font_scaling(self):
        for name in PAGES:
            with self.subTest(page=name):
                self.assertIn("text-size-adjust: 100%", _html(name))

    def test_status_table_scrolls_inside_itself(self):
        html = _html("status")
        self.assertIn("table { display: block; overflow-x: auto;", html)

    def test_comic_admin_buttons_never_stack_characters(self):
        html = _html("comic_admin")
        self.assertIn("button { white-space: nowrap; }", html)

    def test_characters_header_hint_is_clamped(self):
        html = _html("characters")
        self.assertIn("-webkit-line-clamp: 2", html)
        self.assertIn("header .hint {", html)

    def test_moss_studio_hides_long_pill_on_mobile(self):
        html = _html("moss_studio")
        self.assertIn(".topbar .pill, .topbar .spacer { display: none; }", html)

    def test_small_input_pages_use_16px(self):
        for name in ("gen_pad", "compat"):
            with self.subTest(page=name):
                self.assertIn("font-size: 16px;", _html(name))


if __name__ == "__main__":
    unittest.main()
