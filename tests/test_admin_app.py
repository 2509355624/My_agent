# -*- coding: utf-8 -*-
"""管理页公网网关路由测试：登录门槛 + 转发头处理。"""

import json
import unittest
from unittest import mock

import admin_app
from app.core import admin_auth


class FakeResp:
    def __init__(self, content=b"ok", status=200, ctype="application/json",
                 headers=None):
        self.content = content
        self.status_code = status
        self.headers = headers or {"Content-Type": ctype}
        self._closed = False

    def iter_content(self, size):
        yield self.content

    def close(self):
        self._closed = True


class FakeSession:
    last = None

    def __init__(self):
        self.trust_env = True
        self.closed = False

    def request(self, method, url, **kw):
        FakeSession.last = {"method": method, "url": url, "kw": kw}
        return FakeResp()

    def close(self):
        self.closed = True


class AdminAppTest(unittest.TestCase):

    def setUp(self):
        admin_app.app.config["TESTING"] = True
        self.client = admin_app.app.test_client()
        self._rl = dict(admin_app._RL)
        admin_app._RL.clear()

    def tearDown(self):
        admin_app._RL.clear()
        admin_app._RL.update(self._rl)

    def _login(self):
        token = admin_auth.issue_cookie("admin")
        self.client.set_cookie(admin_auth.COOKIE_NAME, token)

    # ── 登录门槛 ──

    def test_login_page_open_without_cookie(self):
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("管理页登录", r.data.decode("utf-8"))

    def test_not_logged_in_redirects_page_to_login(self):
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_not_logged_in_api_returns_401(self):
        r = self.client.get("/api/providers")
        self.assertEqual(r.status_code, 401)

    def test_login_check_sets_cookie(self):
        with mock.patch.object(admin_auth, "configured", return_value=True), \
             mock.patch.object(admin_auth, "verify_password",
                               return_value=(True, "")):
            r = self.client.post("/login/check",
                                 json={"user": "admin", "password": "pw"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["ok"])
        self.assertIn(admin_auth.COOKIE_NAME, r.headers.get("Set-Cookie", ""))

    def test_login_check_rejects_bad_credentials(self):
        with mock.patch.object(admin_auth, "configured", return_value=True), \
             mock.patch.object(admin_auth, "verify_password",
                               return_value=(False, "账号或密码不对")):
            r = self.client.post("/login/check",
                                 json={"user": "admin", "password": "bad"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.json["ok"])

    def test_login_not_configured_503(self):
        with mock.patch.object(admin_auth, "configured", return_value=False):
            r = self.client.post("/login/check",
                                 json={"user": "a", "password": "b"})
        self.assertEqual(r.status_code, 503)

    def test_health_open(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["ok"])

    # ── 转发 ──

    def test_logged_in_forwards_and_strips_cf_headers(self):
        self._login()
        with mock.patch.object(admin_app, "_new_session", return_value=FakeSession()):
            r = self.client.get("/api/providers",
                                headers={"CF-Connecting-IP": "1.2.3.4",
                                         "CF-Ray": "abc"})
        self.assertEqual(r.status_code, 200)
        kw = FakeSession.last["kw"]
        h = {k.lower(): v for k, v in kw["headers"].items()}
        self.assertNotIn("cf-connecting-ip", h)
        self.assertNotIn("cf-ray", h)

    def test_logged_in_forwards_query_string(self):
        self._login()
        with mock.patch.object(admin_app, "_new_session", return_value=FakeSession()):
            self.client.get("/api/history?agent=main&limit=5")
        self.assertEqual(FakeSession.last["kw"]["params"].get("agent"), "main")
        self.assertEqual(FakeSession.last["kw"]["params"].get("limit"), "5")

    def test_post_body_forwarded_as_json(self):
        self._login()
        with mock.patch.object(admin_app, "_new_session", return_value=FakeSession()):
            self.client.post("/api/chat", json={"message": "hi"})
        self.assertEqual(FakeSession.last["kw"]["json"], {"message": "hi"})

    def test_upstream_status_preserved(self):
        self._login()
        fake = FakeSession()
        with mock.patch.object(admin_app, "_new_session", return_value=fake), \
             mock.patch.object(admin_app, "requests", create=True):
            with mock.patch.object(fake, "request",
                                   return_value=FakeResp(b'{"error":"x"}', 404)):
                r = self.client.get("/api/x")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
