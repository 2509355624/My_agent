# -*- coding: utf-8 -*-
"""独立游客页 `/play` 的网关单测（2026-11-06）。

用假的 5174 上游，不碰真实服务：验证隔离标记、页面锁、解除口，以及
「/api/solo/* 强制游客」不会连普通 /persona 页一起误伤。
"""
import os
import unittest
from unittest import mock

import friend_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mark_solo(client):
    """给测试客户端带上隔离标记。

    Werkzeug 3.x 用 `(key, value)`，2.x 用 `(server_name, key, value)`；
    直接塞 `headers={"Cookie": ...}` 在新版会被客户端 cookie jar 吃掉。
    """
    try:
        client.set_cookie("fr_solo", "1")
    except TypeError:
        client.set_cookie("localhost", "fr_solo", "1")


class _FakeUpstream:
    status_code = 200
    content = b"<html><body>persona</body></html>"
    headers = {"Content-Type": "text/html; charset=utf-8"}
    cookies = {}


class _FakeSession:
    """记下最后一次转发调用，好检查 qq 有没有被注入。"""

    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return _FakeUpstream()

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return _FakeUpstream()

    def delete(self, url, **kw):
        self.calls.append(("delete", url, kw))
        return _FakeUpstream()

    def close(self):
        pass


class SoloGatewayTest(unittest.TestCase):
    def setUp(self):
        self.client = friend_app.app.test_client()
        self.fake = _FakeSession()
        p = mock.patch.object(friend_app, "_new_session",
                              return_value=self.fake)
        p.start()
        self.addCleanup(p.stop)

    def _solo(self, path, **kw):
        _mark_solo(self.client)
        return self.client.get(path, **kw)

    def test_play_serves_persona_html_and_sets_marker(self):
        r = self.client.get("/play")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"persona", r.data)
        self.assertIn("fr_solo=1", r.headers.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", r.headers.get("Set-Cookie", ""))

    def test_play_html_alias_also_sets_marker(self):
        r = self.client.get("/play.html")
        self.assertEqual(r.status_code, 200)
        self.assertIn("fr_solo=1", r.headers.get("Set-Cookie", ""))

    def test_other_pages_locked_back_to_play(self):
        for path in ("/", "/index.html", "/gallery", "/gallery.html",
                     "/guest", "/preset", "/workshop", "/compat"):
            r = self._solo(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertTrue(r.headers.get("Location", "").endswith("/play"),
                            path)

    def test_qq_gallery_link_unlocks_solo_and_clears_marker(self):
        """QQ /画廊 发来的链接要进登录页/画廊，不能被 /play 的页面锁劫持。

        2026-11-06 回归：站主点过一次 /play 后，浏览器带着 7 天 fr_solo，
        再点 QQ 给的 /gallery 会被挡回游客页，看起来像「链接发错了」。
        """
        r = self._solo("/gallery?from=qq")
        self.assertEqual(r.status_code, 200)
        cookies = " ".join(r.headers.getlist("Set-Cookie"))
        self.assertIn("fr_solo=", cookies)
        r.close()
        # 标记清掉后，这个浏览器再访问 /gallery 也不该被挡回 /play
        r2 = self.client.get("/gallery")
        self.assertEqual(r2.status_code, 200)
        r2.close()

    def test_qq_gallery_html_alias_also_unlocks(self):
        r = self._solo("/gallery.html?from=qq")
        self.assertEqual(r.status_code, 200)
        r.close()

    def test_qq_unlock_only_applies_to_gallery(self):
        """带 from=qq 不能顺手把首页/预设页也解锁，独立页的孤立性保留。"""
        for path in ("/?from=qq", "/preset?from=qq", "/guest?from=qq"):
            r = self._solo(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertTrue(r.headers.get("Location", "").endswith("/play"),
                            path)
            r.close()

    def test_api_is_not_locked(self):
        r = self._solo("/api/persona/session")
        self.assertNotEqual(r.status_code, 302)

    def test_owner_logged_in_is_not_locked(self):
        with mock.patch.object(friend_app, "_logged_in", return_value=True):
            r = self._solo("/")
        self.assertNotEqual(r.status_code, 302)
        r.close()      # send_from_directory 是直传文件，关掉免得留 ResourceWarning

    def test_exit_clears_marker_and_goes_home(self):
        r = self._solo("/play?exit=1")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers.get("Location", "").endswith("/"))
        self.assertIn("fr_solo=", r.headers.get("Set-Cookie", ""))

    def test_logout_clears_marker(self):
        _mark_solo(self.client)
        r = self.client.post("/logout")
        self.assertEqual(r.status_code, 200)
        cookies = " ".join(r.headers.getlist("Set-Cookie"))
        self.assertIn("fr_solo=", cookies)

    def test_normal_persona_page_has_no_marker(self):
        r = self.client.get("/persona")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("fr_solo", r.headers.get("Set-Cookie", ""))

    def test_login_clears_solo_marker(self):
        """登录成功必须顺手清 fr_solo（2026-11-11）：否则登录态一过期/换
        设备没登录，7 天的游客标记又把人弹回 /play，像「又变回游客界面」。
        """
        _mark_solo(self.client)
        with mock.patch.object(friend_app, "rate_ok", return_value=True), \
             mock.patch.object(friend_app.friend_auth, "verify",
                               return_value=(True, "", "signed-token")):
            r = self.client.post("/login/verify",
                                 json={"qq": "12345", "code": "000000"})
        self.assertEqual(r.status_code, 200)
        cookies = " ".join(r.headers.getlist("Set-Cookie"))
        # 登录 cookie 发下去了，同时 fr_solo 被 delete（Set-Cookie 带 Max-Age=0）
        self.assertIn(friend_app.friend_auth.COOKIE_NAME + "=", cookies)
        self.assertIn("fr_solo=", cookies)
        self.assertIn("Max-Age=0", cookies)

    def test_login_verify_reachable_under_solo_lock(self):
        """带 fr_solo 时登录接口不能被页面锁挡回 /play——否则登录这条路是死的
        （登录成功前不算 _logged_in，会被自己这条锁拦下）。verify 失败应回 400，
        而不是 302。
        """
        _mark_solo(self.client)
        with mock.patch.object(friend_app, "rate_ok", return_value=True), \
             mock.patch.object(friend_app.friend_auth, "verify",
                               return_value=(False, "验证码不对", None)):
            r = self.client.post("/login/verify",
                                 json={"qq": "12345", "code": "000000"})
        self.assertEqual(r.status_code, 400)

    def test_login_send_reachable_under_solo_lock(self):
        """验证码接口同理：被锁在 /play 的浏览器也得能重新登录。"""
        _mark_solo(self.client)
        with mock.patch.object(friend_app, "rate_ok", return_value=True), \
             mock.patch.object(friend_app.friend_auth, "send_code",
                               return_value=(True, "ok")):
            r = self.client.post("/login/send", json={"qq": "12345"})
        self.assertEqual(r.status_code, 200)

    def test_exit_link_on_page_uses_the_real_unlock_route(self):
        """前端「退出游客模式」按钮的 href 必须指向服务端真正的解除口。"""
        with open(os.path.join(ROOT, "web", "persona.html"),
                  encoding="utf-8") as f:
            html = f.read()
        self.assertIn('id="btnExitSolo"', html)
        self.assertIn('href="/play?exit=1"', html)
        # SOLO 分支里要把它露出来（style.display 置空），否则按钮永远藏着
        i = html.index("if (SOLO) {", html.index("bindGalleryLink"))
        block = html[i:i + 800]
        self.assertIn("btnExitSolo", block)
        self.assertIn('exitBtn.style.display = ""', block)


class ForceGuestForwardTest(unittest.TestCase):
    def setUp(self):
        self.client = friend_app.app.test_client()
        _mark_solo(self.client)
        self.fake = _FakeSession()
        p = mock.patch.object(friend_app, "_new_session",
                              return_value=self.fake)
        p.start()
        self.addCleanup(p.stop)

    def _last(self):
        return self.fake.calls[-1][2]

    def test_solo_proxy_drops_login_qq_even_with_cookie(self):
        with mock.patch.object(friend_app.friend_auth, "check_cookie",
                               return_value="12345"):
            r = self.client.post("/api/solo/chat", json={"qq": "999", "text": "hi"})
        self.assertEqual(r.status_code, 200)
        kw = self._last()
        self.assertNotIn("qq", kw["params"])
        self.assertNotIn("qq", kw["json"])
        self.assertEqual(kw["json"]["text"], "hi")

    def test_normal_persona_still_injects_login_qq(self):
        """站主浏览器里带着 fr_solo 也不能让普通 /persona 掉成游客。"""
        with mock.patch.object(friend_app.friend_auth, "check_cookie",
                               return_value="12345"):
            r = self.client.post("/api/persona/chat", json={"text": "hi"})
        self.assertEqual(r.status_code, 200)
        kw = self._last()
        self.assertEqual(kw["params"].get("qq"), "12345")
        self.assertEqual(kw["json"].get("qq"), "12345")

    def test_solo_proxy_targets_persona_backend(self):
        r = self.client.get("/api/solo/session")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/api/persona/session", self.fake.calls[-1][1])


if __name__ == "__main__":
    unittest.main()
