# -*- coding: utf-8 -*-
"""角色扮演页的窄屏入口与右栏分区（2026-11-17）。

用户要求「傻瓜式」：手机端不要再让人猜 ☰ 和「状态」，
顶部直接给 角色 / 聊天 / 状态 三个词；右栏也不要一柱子滑到底，
拆成 状态 与 设置 两个页内分区。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "persona.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class PersonaTabsTest(unittest.TestCase):
    def test_mobile_header_has_three_plain_words(self):
        html = _html()
        self.assertIn('id="btnBurger"', html)
        self.assertIn('>角色</button>', html)
        self.assertIn('id="btnChat"', html)
        self.assertIn('>聊天</button>', html)
        self.assertIn('id="btnStats"', html)
        self.assertIn('>状态</button>', html)
        self.assertNotIn('id="btnBurger">☰</button>', html)

    def test_chat_button_only_shows_on_narrow_screens(self):
        html = _html()
        self.assertIn(".top .chat-btn { display: none; }", html)
        self.assertIn("@media (max-width: 1000px)", html)
        block = html[html.index("@media (max-width: 1000px)"):]
        self.assertIn(".top .chat-btn { display: inline-block; }", block)

    def test_right_panel_is_split_into_status_and_settings(self):
        html = _html()
        self.assertIn('id="side2Tabs"', html)
        self.assertIn('data-s2="status"', html)
        self.assertIn('data-s2="settings"', html)
        self.assertGreaterEqual(html.count('data-s2="status"'), 6)
        self.assertGreaterEqual(html.count('data-s2="settings"'), 6)

    def test_right_panel_defaults_to_status(self):
        html = _html()
        self.assertIn('#side2Tabs[data-s2-tab="status"] ~ [data-s2="settings"] '
                      '{ display: none !important; }', html)
        self.assertIn('#side2Tabs[data-s2-tab="settings"] ~ [data-s2="status"] '
                      '{ display: none !important; }', html)
        # 页面刚打开时由 nav 上的 data-s2-tab 决定；不能给 <aside> 多挂属性——
        # 旧测试用精确字符串定位右栏，属性会把锚点拆掉。
        self.assertIn('<aside class="side2" id="side2">', html)
        self.assertIn('<nav class="side2Tabs" id="side2Tabs" '
                      'data-s2-tab="status"', html)

    def test_three_nav_buttons_stay_in_sync_with_drawers(self):
        html = _html()
        self.assertIn("function syncDrawerTab()", html)
        self.assertIn('[["btnBurger", roles], ["btnStats", stats], '
                      '["btnChat", !stats && !roles]]', html)
        self.assertIn('$("btnChat").onclick = function () { closeDrawer(); '
                      'closeStats(); syncDrawerTab(); };', html)
        self.assertIn('$("btnStats").onclick = openStats;', html)

    def test_right_panel_keeps_all_original_ids(self):
        html = _html()
        for id_ in ("nowBox", "pregBox", "cardBox", "bgBox", "panelBox",
                    "noteBox", "affBox", "customBox", "billEntry", "modelBox"):
            self.assertIn('id="%s"' % id_, html, "右栏少了 %s" % id_)


if __name__ == "__main__":
    unittest.main()
