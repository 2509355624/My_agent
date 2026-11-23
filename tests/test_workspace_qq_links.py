# -*- coding: utf-8 -*-
"""QQ 两条链接指令（/画廊、/漫画台）发出去的地址必须是新界面。

2026-11-23：5175 的 /gallery、/comic 改成 302 到 /workspace/，QQ 侧同步
直接发新地址。这里钉三件事，防止哪天又被改回旧路径：

1. `direct_gen._gallery_text()` / `_comic_studio_text()` 私聊正文里是
   `<隧道>/workspace/?from=qq`；
2. 回执尾巴（`generate_image._receipt_links`）里的画廊同理；
3. 5175 的 /gallery、/comic 仍是 302 到 /workspace/，并且 `?from=qq`
   要原样带过去——否则卡在 /play 里种下的 fr_solo 会把页面锁住。

不真连隧道：`friend_site.url()` 打桩成固定域名；5175 用 Flask
`test_client()` 在内存里跑，不占端口、不影响正在服务的进程。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAKE_URL = "https://example.trycloudflare.com"


class _SiteStub:
    """替掉 app.features.qq.friend_site.url()。"""

    @staticmethod
    def url():
        return FAKE_URL


class QQGalleryTextTest(unittest.TestCase):
    def setUp(self):
        from app.features.qq import friend_site
        from app.features.qq import api as qq_api
        self._url = friend_site.url
        self._ctx = qq_api.current_context
        friend_site.url = _SiteStub.url
        qq_api.current_context = lambda: ("private", "12345")

    def tearDown(self):
        from app.features.qq import friend_site
        from app.features.qq import api as qq_api
        friend_site.url = self._url
        qq_api.current_context = self._ctx

    def test_gallery_text_points_to_workspace(self):
        """5175 群友页的正牌落地页是新界面，QQ 直接发它。"""
        from app.features.image import direct_gen
        text = direct_gen._gallery_text()
        self.assertIn(FAKE_URL + "/workspace/?from=qq", text)
        self.assertNotIn("/gallery?from=qq", text)

    def test_comic_text_points_to_workspace(self):
        from app.features.image import direct_gen
        text = direct_gen._comic_studio_text()
        self.assertIn(FAKE_URL + "/workspace/?from=qq", text)
        self.assertNotIn("/comic?from=qq", text)

    def test_group_still_says_dm(self):
        """群里仍只回一句「私聊发你」，不甩链接（2026-10-30 拍板）。"""
        from app.features.qq import api as qq_api
        from app.features.image import direct_gen
        qq_api.current_context = lambda: ("group", "88888")
        for fn in (direct_gen._gallery_text, direct_gen._comic_studio_text):
            self.assertNotIn(FAKE_URL, fn())

    def test_receipt_link_points_to_workspace(self):
        from app.tools.normal import generate_image
        from app.features.qq import friend_site
        from app.gateways import friend_auth
        old_tok = friend_auth.make_runbench_token
        friend_auth.make_runbench_token = lambda *a, **k: ""
        try:
            text = generate_image._receipt_links("private", "12345")
        finally:
            friend_auth.make_runbench_token = old_tok
        self.assertIn(FAKE_URL + "/workspace/?from=qq", text)
        self.assertNotIn("/gallery?from=qq", text)


class FriendAppRedirectsToWorkspaceTest(unittest.TestCase):
    """5175 的 /gallery /comic 只做 302 跳板（旧收藏还能用，落地是新界面）。"""

    @classmethod
    def setUpClass(cls):
        import friend_app
        cls.app = friend_app.app
        cls.mod = friend_app

    def test_gallery_redirects(self):
        with mock.patch.object(self.mod, "_logged_in", return_value=True):
            with self.app.test_client() as c:
                r = c.get("/gallery?from=qq")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/workspace/")

    def test_comic_redirects(self):
        with mock.patch.object(self.mod, "_logged_in", return_value=True):
            with self.app.test_client() as c:
                r = c.get("/comic?from=qq")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/workspace/")

    def test_html_aliases_redirect_too(self):
        with mock.patch.object(self.mod, "_logged_in", return_value=True):
            with self.app.test_client() as c:
                for u in ("/gallery.html", "/comic.html"):
                    r = c.get(u)
                    self.assertEqual(r.status_code, 302, u)
                    self.assertEqual(r.headers["Location"], "/workspace/", u)

    def test_solo_lock_cleared_once_then_served(self):
        """带 fr_solo + from=qq 打开 /workspace/：先绕一趟清锁，再出页面。

        这一趟必须只有一次——清标记若没置回，就会变成无限重定向。
        """
        with self.app.test_client() as c:
            c.set_cookie(self.mod.SOLO_COOKIE, "1", path="/")
            r = c.get("/workspace/?from=qq")
            self.assertEqual(r.status_code, 302)
            self.assertIn("Set-Cookie", r.headers)
            self.assertNotIn("from=qq", r.headers["Location"])
            r2 = c.get(r.headers["Location"])
            self.assertEqual(r2.status_code, 200)


if __name__ == "__main__":
    unittest.main()
