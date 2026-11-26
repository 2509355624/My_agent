# -*- coding: utf-8 -*-
"""访客模式（2026-11-26）：身份 / 每天 5 张 / 漫画不放 / 隐藏内容看不见。

用户拍板的口径（原话摘录）：

    「访客直接点就进去了，能生图」「每天 5 张，换浏览器就随便他换」
    「加号只有图片生成」「我的漫画作品点进去访客空列表」
    「画廊正常放行，但看不到隐藏的内容」
    「访客模式不需要再给他存聊天记录了」
    「我的作品和收藏隐藏」

钉死几条容易悄悄漂掉的边界：

  1. 身份串必须**签名**：自己编一个 id 不算数（否则改个字符就换新额度）。
  2. 额度按**身份串**记而不是整条 cookie：续签一次不能白送 5 张。
  3. 第 6 张不扣、也不给过；跨天（北京时间）自动回到 5 张。
  4. 访客路径白名单只开工作台真的会调的那四组，生产类接口一律不放。
  5. 5174 侧必须先验内部令牌，再验访客头；伪造的头当没带。
  6. `_seer` 对访客一律 False（隐藏内容不可见），但不改任何数据。
  7. 访客**不写服务端聊天存档**（用户 2026-11-26 后半句改口径）。
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from app.gateways import friend_auth
from app.gateways import visitor


def _reload():
    """每个用例拿一份干净的内存账本。"""
    visitor.reset()
    visitor._CACHE = None


class _FakeReq(object):
    """给 `visitor.current()` 用的最小请求壳。"""

    def __init__(self, headers):
        self.headers = headers


class GuestIdentityTest(unittest.TestCase):
    def setUp(self):
        _reload()
        self.addCleanup(_reload)

    def test_signed_id_roundtrip(self):
        raw = visitor.new_id()
        gid = visitor.check_id(raw)
        self.assertTrue(gid)
        self.assertEqual(len(gid), 16)

    def test_forged_id_rejected(self):
        # 自己编一个 id + 编个过期时间，没有签名 → 不算
        self.assertIsNone(visitor.check_id("deadbeefdeadbeef.9999999999.nope"))
        # 签名对但身份段被换掉
        good = visitor.new_id()
        parts = good.split(".")
        self.assertIsNone(visitor.check_id("ffffffffffffffff.%s.%s"
                                           % (parts[1], parts[2])))

    def test_expired_id_rejected(self):
        gid = "abcdefgh12345678"
        exp = 1000000000                       # 2001 年
        raw = "%s.%d.%s" % (gid, exp, visitor._sign(gid, exp))
        self.assertIsNone(visitor.check_id(raw))

    def test_junk_never_raises(self):
        for junk in (None, "", ".", "..", "a.b.c", "1.2.3.4.5", 123):
            self.assertIsNone(visitor.check_id(junk))

    def test_current_requires_internal_token(self):
        raw = visitor.new_id()
        good = friend_auth.secret()
        # 令牌对 → 认
        with mock.patch("flask.request",
                        _FakeReq({"X-Friend-Token": good,
                                  "X-Friend-Guest": raw})):
            got = visitor.current()
            self.assertTrue(got["id"])
            self.assertEqual(got["raw"], raw)
        # 没令牌 → 不认（哪怕访客头签名是对的）
        with mock.patch("flask.request",
                        _FakeReq({"X-Friend-Guest": raw})):
            self.assertEqual(visitor.current()["id"], "")
        # 令牌错 → 不认
        with mock.patch("flask.request",
                        _FakeReq({"X-Friend-Token": "wrong-token",
                                  "X-Friend-Guest": raw})):
            self.assertEqual(visitor.current()["id"], "")


class GuestQuotaTest(unittest.TestCase):
    def setUp(self):
        _reload()
        self.addCleanup(_reload)
        self.gid = visitor.check_id(visitor.new_id())

    def test_five_then_blocked(self):
        self.assertEqual(visitor.remaining(self.gid), 5)
        for i in range(5):
            ok, left = visitor.consume(self.gid)
            self.assertTrue(ok, "第 %d 张应该放行" % (i + 1))
            self.assertEqual(left, 4 - i)
        ok, left = visitor.consume(self.gid)
        self.assertFalse(ok)
        self.assertEqual(left, 0)
        # 第 6 张被拒之后，计数不能再涨（不能扣成负数）
        self.assertEqual(visitor.snapshot(self.gid)["used"], 5)

    def test_refund_never_negative(self):
        visitor.refund(self.gid, 3)
        self.assertEqual(visitor.remaining(self.gid), 5)

    def test_quota_keyed_by_identity_not_cookie(self):
        """同一个人续签一次 cookie，额度不能被重置。"""
        visitor.consume(self.gid)
        again = visitor.check_id(visitor.new_id())     # 换一条 cookie
        self.assertNotEqual(again, self.gid)
        self.assertEqual(visitor.remaining(self.gid), 4)
        # 换个 id 才是新额度
        self.assertEqual(visitor.remaining(again), 5)

    def test_day_rollover(self):
        visitor.consume(self.gid)
        self.assertEqual(visitor.remaining(self.gid), 4)
        with mock.patch.object(visitor, "_today", return_value="2099-01-01"):
            self.assertEqual(visitor.remaining(self.gid), 5)
            ok, left = visitor.consume(self.gid)
            self.assertTrue(ok)
            self.assertEqual(left, 4)

    def test_unknown_identity_gets_nothing(self):
        self.assertEqual(visitor.remaining(""), 0)
        self.assertEqual(visitor.consume(""), (False, 0))


class GuestPathTest(unittest.TestCase):
    def test_workspace_surface_allowed(self):
        for path in ("/api/gen/channels", "/api/gen/ask", "/api/gen/submit",
                     "/api/gen/status", "/api/gen/img",
                     "/api/gal/list", "/api/gal/img/abc"):
            self.assertTrue(visitor.path_allowed(path, "GET"), path)
        self.assertTrue(visitor.path_allowed("/api/gen/ask", "POST"))

    def test_no_server_archive_for_visitor(self):
        """访客不存聊天记录（2026-11-26 用户改的口径），存档接口整个挡住。"""
        for m in ("GET", "POST", "PATCH", "DELETE"):
            self.assertFalse(visitor.path_allowed("/api/chat/archive", m), m)

    def test_comic_read_only(self):
        self.assertTrue(visitor.path_allowed("/api/comic/tasks", "GET"))
        self.assertTrue(visitor.path_allowed("/api/comic/status", "GET"))
        for sub in ("script", "start", "rerun", "resume", "portrait",
                    "chat_revise", "manual", "upload", "save"):
            p = "/api/comic/%s" % sub
            self.assertFalse(visitor.path_allowed(p, "POST"), p)
        # 读名单里的也不许 POST
        self.assertFalse(visitor.path_allowed("/api/comic/tasks", "POST"))

    def test_out_of_scope_blocked(self):
        """工作台到不了的那些，一个都不开。"""
        for path in ("/api/persona/chat", "/api/outfit/save",
                     "/api/runbench/submit", "/api/raglab/ask",
                     "/api/novel/start", "/api/moss/img"):
            self.assertFalse(visitor.path_allowed(path, "GET"), path)


class GuestGallerySeerTest(unittest.TestCase):
    """`_seer` 对访客一律 False：隐藏内容看不见，但一行数据都不动。"""

    def setUp(self):
        from app.features.gallery import routes as gal
        self.gal = gal
        _reload()
        self.addCleanup(_reload)

    def test_visitor_cannot_see_hidden(self):
        raw = visitor.new_id()
        good = friend_auth.secret()
        with mock.patch("flask.request",
                        _FakeReq({"X-Friend-Token": good,
                                  "X-Friend-Guest": raw})):
            with mock.patch.object(self.gal, "_jf", return_value=False):
                # 就算这个人挂了站主的 qq，访客身份优先
                self.assertFalse(self.gal._seer("10001"))

    def test_forged_guest_header_ignored(self):
        """伪造的访客头不算数 → 回到原来那条路（这里给了站主号就该看见）。"""
        with mock.patch("flask.request",
                        _FakeReq({"X-Friend-Token": "wrong",
                                  "X-Friend-Guest": visitor.new_id()})):
            with mock.patch.object(self.gal, "_jf", return_value=False):
                with mock.patch.object(self.gal, "_is_owner",
                                       return_value=True):
                    self.assertTrue(self.gal._seer("10001"))


if __name__ == "__main__":
    unittest.main()
