# -*- coding: utf-8 -*-
"""`web/persona_admin.html`（查看台）的静态体检（2026-11-09）。

用户原话：「查看页也给我角色说的话高亮吧，不然看的怪难受的」——回看是一整屏
散文，她说出口的话夹在叙述里扫不出来。这里钉三件事，防止以后改渲染时回退：

1. 台词/心声的样式在；
2. 上色走 `fmtDlg`，**先转义再包 span**（这条是安全边界，不是样式问题）；
3. `makeMsg` 不许绕过 `fmtDlg` 直接塞原文。
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "persona_admin.html")


def _read():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class DialogHighlightTest(unittest.TestCase):
    def setUp(self):
        self.html = _read()

    def test_styles_exist(self):
        self.assertIn(".msg .b .dlg", self.html)
        self.assertIn(".msg .b .mono", self.html)

    def test_escape_runs_before_wrapping(self):
        fn = self.html[self.html.index("function fmtDlg("):]
        fn = fn[:fn.index("\n}\n") + 3]
        esc_at = fn.index('.replace(/&/g, "&amp;")')
        span_at = fn.index('<span class="dlg">')
        self.assertLess(esc_at, span_at, "转义必须排在包 span 之前，否则等于放行 HTML")
        # 五种转义一个都不能少：漏 `>` 或引号就等于给出注入面。
        for out in ('"&amp;"', '"&lt;"', '"&gt;"', '"&quot;"', '"&#39;"'):
            self.assertIn(out, fn)

    def test_make_msg_goes_through_fmt_dlg(self):
        body = self.html[self.html.index("function makeMsg("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("fmtDlg(", body)
        self.assertNotIn("m.text || \"\")", body.replace("fmtDlg(m.text || \"\")", ""))


class MobileDrawerTest(unittest.TestCase):
    """2026-11-16：手机端左列表抽屉 + 右完整记录占满屏。

    钉住几件容易被改回的事：☰/遮罩存在、桌面隐藏、抽屉 class 开关、
    选中会话后收起、桌面两列布局没被动。
    """

    def setUp(self):
        self.html = _read()

    def test_menu_and_mask_exist(self):
        self.assertIn('id="menuBtn"', self.html)
        self.assertIn('id="mask"', self.html)

    def test_drawer_only_on_mobile(self):
        self.assertIn("@media (max-width: 820px)", self.html)
        self.assertIn("#menuBtn { display: none; }", self.html)
        self.assertIn("body.drawer .side { transform: none; }", self.html)
        self.assertIn("body.drawer #mask { display: block; }", self.html)

    def test_desktop_two_column_untouched(self):
        self.assertIn("grid-template-columns: 360px 1fr", self.html)
        self.assertIn(".app { grid-template-columns: 1fr; }", self.html)

    def test_open_closes_drawer(self):
        fn = self.html[self.html.index("async function open(r)"):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("closeDrawer()", fn)

    def test_toggle_and_mask_wiring(self):
        self.assertIn('$("menuBtn").onclick', self.html)
        self.assertIn('$("mask").onclick = closeDrawer', self.html)


if __name__ == "__main__":
    unittest.main()
