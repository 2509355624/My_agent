# -*- coding: utf-8 -*-
"""QQ 三个入口各指各的：/画廊、/漫画台、/工作台 不能互相串门。

**2026-11-25 用户纠正**：在此之前 `/画廊` 和 `/漫画台` 都被改成发
`/workspace/`（新界面），于是三个不同的地方指向同一个页面。用户把定位理清了：

    画廊   → 作品墙（自定义预设 / 提示词工坊 / 角色扮演 / 漫画台 / 小说
             都收在那一边）
    漫画台 → 群友版漫画台
    工作台 → 新界面聊天页 `/workspace/`

这里钉四件事，防止哪天又被改回「三个都发 workspace」：

1. 三条指令的私聊正文各自拼对路径；
2. 回执尾巴里的「画廊」真的是画廊，「工作台」单列一行；
3. 5175 的 `/gallery`、`/comic` 直接出页面（不再 302 到工作台）；
4. `?from=qq` 仍能解开 /play 页面锁——只是绕回自身，不是跳去工作台。

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

    def test_gallery_text_points_to_gallery(self):
        """`/画廊` 发画廊，不是工作台。"""
        from app.features.image import direct_gen
        text = direct_gen._gallery_text()
        self.assertIn(FAKE_URL + "/gallery?from=qq", text)
        self.assertNotIn("/workspace/", text, "画廊不该再指到工作台")

    def test_comic_text_points_to_comic(self):
        """`/漫画台` 发群友版漫画台，不是工作台。"""
        from app.features.image import direct_gen
        text = direct_gen._comic_studio_text()
        self.assertIn(FAKE_URL + "/comic?from=qq", text)
        self.assertNotIn("/workspace/", text, "漫画台不该再指到工作台")

    def test_workbench_text_points_to_workspace(self):
        """`/工作台` 才是新界面。"""
        from app.features.image import direct_gen
        text = direct_gen._workbench_text()
        self.assertIn(FAKE_URL + "/workspace/?from=qq", text)

    def test_three_entries_never_share_a_url(self):
        from app.features.image import direct_gen
        urls = {direct_gen._gallery_text().splitlines()[0],
                direct_gen._comic_studio_text().splitlines()[0],
                direct_gen._workbench_text().splitlines()[0]}
        self.assertEqual(len(urls), 3, "三个入口不能指同一个地址")

    def test_group_still_says_dm(self):
        """群里仍只回一句「私聊发你」，不甩链接（2026-10-30 拍板）。"""
        from app.features.qq import api as qq_api
        from app.features.image import direct_gen
        qq_api.current_context = lambda: ("group", "88888")
        for fn in (direct_gen._gallery_text, direct_gen._comic_studio_text,
                   direct_gen._workbench_text):
            self.assertNotIn(FAKE_URL, fn())

    def test_receipt_links_name_the_right_pages(self):
        from app.tools.normal import generate_image
        from app.gateways import friend_auth
        old_tok = friend_auth.make_runbench_token
        friend_auth.make_runbench_token = lambda *a, **k: ""
        try:
            text = generate_image._receipt_links("private", "12345")
        finally:
            friend_auth.make_runbench_token = old_tok
        self.assertIn("画廊 " + FAKE_URL + "/gallery?from=qq", text)
        self.assertIn("工作台 " + FAKE_URL + "/workspace/?from=qq", text)


class FriendAppServesRealPagesTest(unittest.TestCase):
    """5175 的 /gallery /comic 直接出页面，不再 302 到工作台。"""

    @classmethod
    def setUpClass(cls):
        import friend_app
        cls.app = friend_app.app
        cls.mod = friend_app

    def test_gallery_serves_the_wall(self):
        with mock.patch.object(self.mod, "_logged_in", return_value=True):
            with self.app.test_client() as c:
                r = c.get("/gallery")
        self.assertEqual(r.status_code, 200)
        self.assertIn("画廊", r.get_data(as_text=True)[:4000])

    def test_comic_serves_the_friend_studio(self):
        with mock.patch.object(self.mod, "_logged_in", return_value=True):
            with self.app.test_client() as c:
                r = c.get("/comic")
        self.assertEqual(r.status_code, 200)
        self.assertIn("漫画工作台", r.get_data(as_text=True)[:4000])

    def test_html_aliases_serve_too(self):
        with mock.patch.object(self.mod, "_logged_in", return_value=True):
            with self.app.test_client() as c:
                for u in ("/gallery.html", "/comic.html"):
                    r = c.get(u)
                    self.assertEqual(r.status_code, 200, u)

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

    def test_gallery_solo_lock_clears_by_redirecting_to_itself(self):
        """`/gallery?from=qq` 现在直接出页面，清锁得靠绕回自身。

        以前这里 `return None` 是因为「/gallery 视图自己会 302」，2026-11-25
        改成直接发作品墙之后，再 `return None` 就等于 cookie 没清、还挂着
        fr_solo，下一趟就会被挡回 /play。
        """
        with self.app.test_request_context("/gallery?from=qq"):
            with mock.patch.object(self.mod, "_solo_in", return_value=True), \
                    mock.patch.object(self.mod, "_logged_in",
                                      return_value=False):
                out = self.mod._gate()
        self.assertEqual(out.status_code, 302)
        self.assertEqual(out.headers["Location"], "/gallery")

    def test_comic_solo_lock_clears_by_redirecting_to_itself(self):
        with self.app.test_request_context("/comic?from=qq"):
            with mock.patch.object(self.mod, "_solo_in", return_value=True), \
                    mock.patch.object(self.mod, "_logged_in",
                                      return_value=False):
                out = self.mod._gate()
        self.assertEqual(out.status_code, 302)
        self.assertEqual(out.headers["Location"], "/comic")

    def test_providers_never_leaks_keys(self):
        """群友版漫画台要 `/api/providers` 拉下拉，但密钥和 base_url 是站主的。"""
        with self.app.test_client() as c, \
                mock.patch.object(self.mod.requests, "get") as m:
            m.return_value.json.return_value = {
                "providers": [{"id": "a", "label": "A", "model": "m",
                               "base_url": "https://secret", "key_tail": "1234"}],
                "default": "a"}
            d = c.get("/api/providers").get_json()
        self.assertEqual(sorted(d["providers"][0]), ["id", "label", "model"])

    def test_models_refuses_without_provider(self):
        """不带 provider 的那条会按 baseUrl 连任意地址，不能开给公网。"""
        with self.app.test_client() as c:
            self.assertEqual(c.get("/api/models").status_code, 400)


if __name__ == "__main__":
    unittest.main()
