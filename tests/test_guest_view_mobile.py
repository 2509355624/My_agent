# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "guest_view.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class GuestViewMobileTest(unittest.TestCase):
    def test_mobile_header_has_a_more_menu(self):
        html = _html()
        self.assertIn('id="gMoreBtn"', html)
        self.assertIn('id="gTools"', html)
        self.assertIn("header .gtools.open { display: flex; }", html)
        self.assertIn("tools.classList.toggle('open')", html)

    def test_drawer_button_is_named(self):
        html = _html()
        self.assertIn('id="menuBtn" aria-label="会话列表">会话</button>', html)
        self.assertNotIn("☰", html)

    def test_login_is_short(self):
        html = _html()
        self.assertIn("只读。管理员 QQ 登录。", html)
        self.assertIn("font-size: 16px; color: var(--text); background: var(--bg-input);", html)

    def test_core_ids_are_kept(self):
        html = _html()
        for element_id in ("qq", "code", "sendBtn", "verifyBtn", "msg", "app",
                            "menuBtn", "title", "fsDown", "fsUp", "autoBtn",
                            "exportBtn", "logoutBtn", "list", "detail",
                            "dayFilter", "gMoreBtn", "gTools"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
