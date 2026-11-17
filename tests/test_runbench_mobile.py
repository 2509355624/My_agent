# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "runbench.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class RunbenchSimpleTest(unittest.TestCase):
    def test_no_pink_purple_gradient_on_buttons(self):
        html = _html()
        self.assertNotIn("linear-gradient(135deg,var(--accent),var(--accent2))", html)
        self.assertIn("background:var(--accent);", html)

    def test_head_is_short_and_links_home(self):
        html = _html()
        self.assertIn('<a class="back" href="/">← 回首页</a>', html)
        self.assertIn("点卡片，写一句需求，出图发回 QQ。", html)

    def test_mobile_dialog_input_is_16px(self):
        html = _html()
        self.assertIn(".dlg textarea { font-size: 16px; }", html)

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("cntChannel", "cntPreset", "channelGrid", "presetGrid",
                            "status", "mask", "dTitle", "dSub", "upBox",
                            "upPreview", "upTxt", "upRm", "btnPaste", "dPrompt",
                            "okBar", "dCancel", "dGo", "upFile"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
