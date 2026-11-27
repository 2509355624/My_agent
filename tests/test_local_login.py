# -*- coding: utf-8 -*-
"""测试账号登录（`/login/local`）：面试演示用的那条免 QQ 支路。

2026-11-27：站主要一个演示能用的账号——账号 + 密码，不走 QQ 好友白名单、
不收验证码。凭据只从 `.env` 的 `LOCAL_LOGIN` 读：仓库是 public，源码里出现
明文等于把钥匙挂在门上。这里钉四件事：

  · 没配就没有这个入口（换台机器不配 = 门关着）；
  · 配了也要账号密码都对得上；
  · 登录后的身份是假 QQ 号，`check_cookie` 认得回来（后面所有按 qq 记账的
    链路才不用另开支路）；
  · 连错会锁，而且只锁测试账号自己——不能顺手把同号的真 QQ 用户锁掉。
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from app.gateways import friend_auth


class LocalAccountParseTest(unittest.TestCase):
    def _env(self, val):
        return mock.patch.dict(os.environ, {"LOCAL_LOGIN": val})

    def test_not_configured_is_closed(self):
        with self._env(""):
            self.assertEqual(friend_auth.local_account(), ("", ""))
            ok, _, tok = friend_auth.local_login("a", "b")
            self.assertFalse(ok, "没配就不该放人进来")
            self.assertIsNone(tok)

    def test_parses_user_and_password(self):
        with self._env("demo:change_me"):
            self.assertEqual(friend_auth.local_account(), ("demo", "change_me"))

    def test_bad_shapes_are_closed(self):
        """没冒号 / 半边长 / 全空白：一律当没配，别拿半条配置去比。"""
        for bad in ("demo", "demo:", ":change_me", "  :  ", ""):
            with self._env(bad):
                self.assertEqual(friend_auth.local_account(), ("", ""), bad)

    def test_password_may_contain_colon(self):
        """只按**第一个**冒号切，密码里带冒号不该被截断。"""
        with self._env("demo:a:b"):
            self.assertEqual(friend_auth.local_account(), ("demo", "a:b"))


class LocalLoginTest(unittest.TestCase):
    def setUp(self):
        # 登录账本（codes/sent/fails）不能写到真的 state/friend_auth.json 去。
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(friend_auth, "_AUTH",
                                    os.path.join(self.tmp, "auth.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        friend_auth.reset()
        self.addCleanup(friend_auth.reset)

    def test_ok_issues_cookie_for_local_qq(self):
        with mock.patch.dict(os.environ, {"LOCAL_LOGIN": "demo:pw"}):
            ok, msg, tok = friend_auth.local_login("demo", "pw")
            self.assertTrue(ok, msg)
            self.assertEqual(friend_auth.check_cookie(tok),
                             friend_auth.LOCAL_QQ)

    def test_wrong_password_rejected(self):
        with mock.patch.dict(os.environ, {"LOCAL_LOGIN": "demo:pw"}):
            ok, msg, tok = friend_auth.local_login("demo", "nope")
            self.assertFalse(ok)
            self.assertIn("不对", msg)
            self.assertIsNone(tok, "错了就不给 cookie")

    def test_local_qq_passes_qq_re(self):
        """假号必须过 `QQ_RE`，否则 `check_cookie` / `_who()` 一路都不认它。"""
        self.assertTrue(friend_auth.QQ_RE.match(friend_auth.LOCAL_QQ))

    def test_failures_do_not_touch_real_qq_buckets(self):
        with mock.patch.dict(os.environ, {"LOCAL_LOGIN": "demo:pw"}):
            for _ in range(friend_auth.TRY_MAX):
                friend_auth.local_login("demo", "nope")
            self.assertGreater(friend_auth._locked("local:demo"), 0)
            self.assertEqual(friend_auth._locked("demo"), 0,
                             "真 QQ 号那本账不能被连坐")
            self.assertEqual(friend_auth._locked(friend_auth.LOCAL_QQ), 0)

    def test_locked_blocks_even_correct_password(self):
        with mock.patch.dict(os.environ, {"LOCAL_LOGIN": "demo:pw"}):
            for _ in range(friend_auth.TRY_MAX):
                friend_auth.local_login("demo", "nope")
            ok, msg, _ = friend_auth.local_login("demo", "pw")
            self.assertFalse(ok)
            self.assertIn("分钟", msg)


if __name__ == "__main__":
    unittest.main()
