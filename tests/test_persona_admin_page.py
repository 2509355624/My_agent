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


if __name__ == "__main__":
    unittest.main()
