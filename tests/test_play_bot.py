# -*- coding: utf-8 -*-
"""`/玩法` 卡片指令的单测（2026-11-12）。

地址每次都从 `friend_site.url()` 现读，所以这里全部 mock 掉，不依赖隧道
是否真的开着；卡片发送也 mock 掉，不碰 QQ 协议端。
"""
import unittest
from unittest import mock

from app import play_bot

URL = "https://abc.trycloudflare.com"


class CommandRegexTest(unittest.TestCase):
    def test_hits(self):
        for s in ("/玩法", "  ／ 玩法", "大大怪 /玩法"):
            self.assertTrue(play_bot.PLAY_RE.search(s), s)

    def test_misses(self):
        for s in ("玩法", "/画廊", "随便聊聊", "/玩", ""):
            self.assertFalse(play_bot.PLAY_RE.search(s), s)


class HeadTextTest(unittest.TestCase):
    def test_open_puts_gallery_first(self):
        """画廊链接必须顶在开头。"""
        with mock.patch("app.friend_site.url", return_value=URL):
            head = play_bot.head_text()
        self.assertTrue(head.startswith("🎮 玩法"))
        self.assertIn(URL + "/gallery?from=qq", head)
        # 画廊链接出现在「不提渠道就跑默认」之前
        self.assertLess(head.index(URL), head.index("不提渠道"))

    def test_closed_explains_without_stale_link(self):
        with mock.patch("app.friend_site.url", return_value=""):
            head = play_bot.head_text()
        self.assertNotIn("trycloudflare", head)
        self.assertIn("隧道现在没开", head)


class FamilyNodesTest(unittest.TestCase):
    def test_every_family_is_a_node(self):
        """标题节点 + 每个家族一个节点，节点数 = 家族数 + 1。"""
        with mock.patch("app.friend_site.url", return_value=URL), \
             mock.patch.object(play_bot, "_bot_identity",
                               return_value=("大大怪", "10000")):
            nodes = play_bot.build_nodes()
        self.assertEqual(len(nodes), len(play_bot._FAMILIES) + 1)
        self.assertTrue(all(n["type"] == "node" for n in nodes))
        self.assertEqual(nodes[0]["data"]["name"], "大大怪")

    def test_default_and_qwen_present(self):
        with mock.patch("app.friend_site.url", return_value=URL), \
             mock.patch.object(play_bot, "_bot_identity",
                               return_value=("大大怪", "10000")):
            nodes = play_bot.build_nodes()
        joined = "\n".join(n["data"]["content"][0]["data"]["text"]
                           for n in nodes)
        self.assertIn("默认", joined)
        self.assertIn("silver", joined)
        self.assertIn("Qwen", joined)
        self.assertIn("qwen修复", joined)


class PlainTextTest(unittest.TestCase):
    def test_plain_matches_card_grouping(self):
        """纯文本兜底的分组必须和卡片一致。"""
        with mock.patch("app.friend_site.url", return_value=URL):
            plain = play_bot.plain_text()
        self.assertIn(URL + "/gallery?from=qq", plain)
        for title, _ in play_bot._FAMILIES:
            self.assertIn("【%s】" % title, plain)


class SendTest(unittest.TestCase):
    def test_group_uses_card(self):
        with mock.patch("app.friend_site.url", return_value=URL), \
             mock.patch.object(play_bot.qq_api, "send_forward") as fw, \
             mock.patch.object(play_bot.qq_api, "send_group") as sg:
            ok = play_bot.send("group", "123")
        self.assertTrue(ok)
        self.assertEqual(fw.call_args[0][:2], ("group", "123"))
        sg.assert_not_called()

    def test_private_card_failure_falls_back_to_text(self):
        with mock.patch("app.friend_site.url", return_value=URL), \
             mock.patch.object(play_bot.qq_api, "send_forward",
                               side_effect=RuntimeError("协议端不支持")), \
             mock.patch.object(play_bot.qq_api, "send_private") as sp:
            ok = play_bot.send("private", "456")
        self.assertTrue(ok)
        sp.assert_called_once()
        self.assertIn(URL + "/gallery?from=qq", sp.call_args[0][1])

    def test_both_fail_returns_false(self):
        with mock.patch("app.friend_site.url", return_value=URL), \
             mock.patch.object(play_bot.qq_api, "send_forward",
                               side_effect=RuntimeError("x")), \
             mock.patch.object(play_bot.qq_api, "send_private",
                               side_effect=RuntimeError("y")):
            self.assertFalse(play_bot.send("private", "456"))


if __name__ == "__main__":
    unittest.main()
