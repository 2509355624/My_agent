# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "friend.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class FriendLoginSimpleTest(unittest.TestCase):
    def test_route_title_is_kept(self):
        self.assertIn("<title>大大怪 · 生图页</title>", _html())

    def test_login_is_short(self):
        html = _html()
        self.assertIn("<h2>登录</h2>", html)
        self.assertIn("填 QQ，点「获取验证码」，验证码会私聊发你。", html)
        self.assertIn('href="/"', html)  # 保留回画廊的路

    def test_no_emoji_in_entries(self):
        html = _html()
        for emoji in ("🖼️", "🎨", "💬"):
            self.assertNotIn(emoji, html)

    def test_mobile_inputs_are_16px(self):
        self.assertIn("input[type=text], input[type=number] { font-size: 16px; }",
                      _html())

    def test_core_ids_are_kept(self):
        html = _html()
        for element_id in ("login", "entGallery", "qq", "code", "sendBtn",
                            "verifyBtn", "loginMsg", "app", "slots", "prompt",
                            "goBtn", "logoutBtn"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
