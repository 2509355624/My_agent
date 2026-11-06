# -*- coding: utf-8 -*-
"""`#游客模式` 卡片指令的单测（2026-11-06）。

地址每次都从 `friend_site.url()` 现读，所以这里全部 mock 掉，不依赖隧道
是否真的开着；卡片发送也 mock 掉，不碰 QQ 协议端。
"""
import unittest
from unittest import mock

from app import solo_bot

URL = "https://abc.trycloudflare.com"


class CommandRegexTest(unittest.TestCase):
    def test_hits(self):
        for s in ("#游客模式", "  ＃ 游客模式", "大大怪 #游客模式"):
            self.assertTrue(solo_bot.SOLO_RE.search(s), s)

    def test_misses(self):
        for s in ("游客模式", "#画廊", "随便聊聊", "#游客", ""):
            self.assertFalse(solo_bot.SOLO_RE.search(s), s)


class LinkTextTest(unittest.TestCase):
    def test_open(self):
        with mock.patch("app.friend_site.url", return_value=URL + "/"):
            self.assertEqual(solo_bot.link_text(), URL + "/play")

    def test_closed_returns_empty(self):
        with mock.patch("app.friend_site.url", return_value=""):
            self.assertEqual(solo_bot.link_text(), "")


class CardTextTest(unittest.TestCase):
    def test_open_has_link_and_rules(self):
        with mock.patch("app.friend_site.url", return_value=URL), \
             mock.patch.object(solo_bot, "_bot_identity",
                               return_value=("大大怪", "10000")):
            parts = solo_bot.card_parts()
            nodes = solo_bot.build_nodes()
            plain = solo_bot.plain_text()
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[1], URL + "/play")
        self.assertIn("30 轮", parts[2])
        self.assertIn("登录入口", parts[2])
        self.assertEqual(len(nodes), len(parts))
        self.assertTrue(all(n["type"] == "node" for n in nodes))
        self.assertIn(URL + "/play", str(nodes[1]))
        self.assertIn(URL + "/play", plain)

    def test_closed_explains_without_stale_link(self):
        with mock.patch("app.friend_site.url", return_value=""):
            parts = solo_bot.card_parts()
            plain = solo_bot.plain_text()
        self.assertEqual(len(parts), 2)
        self.assertIn("隧道现在没开", parts[1])
        self.assertNotIn("trycloudflare", plain)
        self.assertIn("启动群友隧道", plain)

    def test_card_is_always_forward_not_bare_text(self):
        """卡片是合并转发节点，链接单独一个节点方便长按复制。"""
        with mock.patch("app.friend_site.url", return_value=URL), \
             mock.patch.object(solo_bot, "_bot_identity",
                               return_value=("大大怪", "10000")):
            nodes = solo_bot.build_nodes()
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[0]["data"]["name"], "大大怪")


class SendTest(unittest.TestCase):
    def test_group_uses_card(self):
        with mock.patch("app.friend_site.url", return_value=URL), \
             mock.patch.object(solo_bot.qq_api, "send_forward") as fw, \
             mock.patch.object(solo_bot.qq_api, "send_group") as sg:
            ok = solo_bot.send("group", "123")
        self.assertTrue(ok)
        self.assertEqual(fw.call_args[0][:2], ("group", "123"))
        sg.assert_not_called()

    def test_private_card_failure_falls_back_to_text(self):
        with mock.patch("app.friend_site.url", return_value=URL), \
             mock.patch.object(solo_bot.qq_api, "send_forward",
                               side_effect=RuntimeError("协议端不支持")), \
             mock.patch.object(solo_bot.qq_api, "send_private") as sp:
            ok = solo_bot.send("private", "456")
        self.assertTrue(ok)
        sp.assert_called_once()
        self.assertIn(URL + "/play", sp.call_args[0][1])

    def test_both_fail_returns_false(self):
        with mock.patch("app.friend_site.url", return_value=URL), \
             mock.patch.object(solo_bot.qq_api, "send_forward",
                               side_effect=RuntimeError("x")), \
             mock.patch.object(solo_bot.qq_api, "send_private",
                               side_effect=RuntimeError("y")):
            self.assertFalse(solo_bot.send("private", "456"))


if __name__ == "__main__":
    unittest.main()
