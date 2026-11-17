# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "persona_admin.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class PersonaAdminMobileTest(unittest.TestCase):
    def test_mobile_topbar_has_a_more_menu(self):
        html = _html()
        self.assertIn('id="pMoreBtn"', html)
        self.assertIn('id="pTools"', html)
        self.assertIn(".top .tools.open { display: flex; }", html)
        self.assertIn("tools.classList.toggle('open')", html)

    def test_drawer_button_is_named(self):
        html = _html()
        self.assertIn('id="menuBtn" aria-label="会话列表" title="展开会话列表">会话</button>', html)
        self.assertIn("点「会话」选一个人。", html)

    def test_links_to_home_and_novel(self):
        html = _html()
        self.assertIn('<a class="back" href="/">← 首页</a>', html)
        self.assertIn("/novel_admin", html)

    def test_ids_are_kept(self):
        html = _html()
        for element_id in ("cnt", "menuBtn", "who", "meta", "fsDown", "fsVal",
                            "fsUp", "fsReset", "btnBilling", "auto", "refresh",
                            "btnCard", "del", "gvQq", "gvAddBtn", "list", "log",
                            "jump", "toast", "pMoreBtn", "pTools"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
