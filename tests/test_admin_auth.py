# -*- coding: utf-8 -*-
"""管理页公网网关的鉴权测试：账号密码 + HMAC cookie。"""

import os
import tempfile
import unittest
from unittest import mock

from app import admin_auth


class AdminAuthTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="adm_")
        self.auth_file = os.path.join(self.tmp, "admin_auth.json")
        self.secret_file = os.path.join(self.tmp, "admin_secret")
        self.p1 = mock.patch.object(admin_auth, "_AUTH", self.auth_file)
        self.p2 = mock.patch.object(admin_auth, "_SECRET", self.secret_file)
        self.p3 = mock.patch.object(admin_auth, "_secret_cache", None)
        self.p1.start(); self.p2.start(); self.p3.start()
        self.addCleanup(self.p1.stop); self.addCleanup(self.p2.stop)
        self.addCleanup(self.p3.stop)

    def test_no_account_fail_closed(self):
        self.assertFalse(admin_auth.configured())
        ok, _ = admin_auth.verify_password("admin", "x")
        self.assertFalse(ok)

    def test_set_password_and_verify(self):
        admin_auth.set_password("admin", "S3cret!pass")
        self.assertTrue(admin_auth.configured())
        ok, msg = admin_auth.verify_password("admin", "S3cret!pass")
        self.assertTrue(ok, msg)
        ok, _ = admin_auth.verify_password("admin", "wrong")
        self.assertFalse(ok)
        ok, _ = admin_auth.verify_password("other", "S3cret!pass")
        self.assertFalse(ok)

    def test_password_not_stored_in_plain_text(self):
        admin_auth.set_password("admin", "PlainTextP@ss")
        with open(self.auth_file, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("PlainTextP@ss", raw)
        self.assertIn("hash", raw)
        self.assertIn("salt", raw)

    def test_cookie_roundtrip(self):
        admin_auth.set_password("admin", "pw")
        token = admin_auth.issue_cookie("admin")
        self.assertEqual(admin_auth.check_cookie(token), "admin")

    def test_cookie_tampered_rejected(self):
        admin_auth.set_password("admin", "pw")
        token = admin_auth.issue_cookie("admin")
        bad = token[:-1] + ("0" if token[-1] != "0" else "1")
        self.assertIsNone(admin_auth.check_cookie(bad))
        self.assertIsNone(admin_auth.check_cookie(""))
        self.assertIsNone(admin_auth.check_cookie("a.b.c"))

    def test_cookie_expired_rejected(self):
        admin_auth.set_password("admin", "pw")
        with mock.patch.object(admin_auth, "_now", return_value=1000):
            token = admin_auth.issue_cookie("admin")
        with mock.patch.object(admin_auth, "_now", return_value=1000 + admin_auth.COOKIE_TTL + 1):
            self.assertIsNone(admin_auth.check_cookie(token))

    def test_change_password_invalidates_old_cookie(self):
        admin_auth.set_password("admin", "pw1")
        token = admin_auth.issue_cookie("admin")
        self.assertEqual(admin_auth.check_cookie(token), "admin")
        admin_auth.set_password("admin", "pw2")
        self.assertIsNone(admin_auth.check_cookie(token))


if __name__ == "__main__":
    unittest.main()
