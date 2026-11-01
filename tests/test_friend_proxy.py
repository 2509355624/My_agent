# -*- coding: utf-8 -*-
"""5175 转发层：大图必须流式、Range 必须透传、qq 不能被客户端改。

这几条是「下载原图」的地基。转发一旦退回 `r.content`，一张 26MB 的图会在
5175 里多占一份内存，几个人同时下就把这个进程顶爆；Range 不转发则断点续传
和下载器分段全部退化成整包重传。
"""

import gzip
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import friend_app                       # noqa: E402
from app import friend_auth             # noqa: E402


class _FakeResp(object):
    """够 `_forward` 用的最小响应：headers / iter_content / content / close。"""

    def __init__(self, body=b"0123456789" * 100, ctype="image/png", status=200,
                 extra=None):
        self.status_code = status
        self.content = body
        self.headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
        self.headers.update(extra or {})
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]

    def close(self):
        self.closed = True


class StreamForwardTest(unittest.TestCase):

    def setUp(self):
        self.client = friend_app.app.test_client()
        self.client.set_cookie("fr_sess", friend_auth.issue_cookie("2509355624"))

    def _get(self, path, headers=None, resp=None):
        exp = resp or _FakeResp()
        with patch.object(friend_app._SESSION, "get", return_value=exp) as m:
            r = self.client.get(path, headers=headers or {})
        return r, exp, m

    def test_image_download_is_streamed(self):
        """图片走 stream=True——否则整包读进 5175 内存。"""
        r, exp, m = self._get("/api/gal/img/7?s=full")
        self.assertTrue(m.call_args.kwargs.get("stream"))
        # 生成器要真的被消费才算走完流；消费完必须把上游连接关掉，
        # 否则客户端中途取消时 requests 的连接会一直挂着。
        self.assertEqual(len(r.data), len(exp.content))
        self.assertTrue(exp.closed, "流式响应结束后必须关上游连接")

    def test_non_image_api_is_not_streamed(self):
        """接口响都很小，流式没收益，别多一层生成器。"""
        _, _, m = self._get("/api/gal/list")
        self.assertFalse(m.call_args.kwargs.get("stream"))

    def test_range_header_is_forwarded(self):
        """断点续传靠 Range 透传：不转发就变成每次整包重传。"""
        resp = _FakeResp(status=206, extra={
            "Content-Range": "bytes 0-99/1000", "Accept-Ranges": "bytes"})
        r, _, m = self._get("/api/gal/img/7?s=full",
                            headers={"Range": "bytes=0-99"}, resp=resp)
        self.assertEqual(m.call_args.kwargs["headers"]["Range"], "bytes=0-99")
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.headers.get("Content-Range"), "bytes 0-99/1000")

    def test_length_and_accept_ranges_survive(self):
        """少 Content-Length 浏览器没有进度，少 Accept-Ranges 续传直接坏。"""
        resp = _FakeResp(status=206, extra={"Accept-Ranges": "bytes"})
        r, _, _ = self._get("/api/gal/img/7?s=full",
                            headers={"Range": "bytes=0-99"}, resp=resp)
        self.assertEqual(r.headers.get("Content-Length"), "1000")
        self.assertEqual(r.headers.get("Accept-Ranges"), "bytes")

    def test_cookie_qq_wins_over_query_qq(self):
        """改 URL 里的 qq 不能看别人的东西——一律用登录态里的号。"""
        _, _, m = self._get("/api/gal/img/7?s=full&qq=123456")
        self.assertEqual(m.call_args.kwargs["params"]["qq"], "2509355624")

    def test_not_logged_in_gets_401(self):
        anon = friend_app.app.test_client()
        r = anon.get("/api/gal/img/7?s=full")
        self.assertEqual(r.status_code, 401)


class PageRoutingTest(unittest.TestCase):
    """`/` = 画廊，`/preset` = 自定义预设（2026-11-01 用户拍板）。

    用户原话：「我希望进去之后就是默认画廊，然后右上角才是自定义预设的功能，
    因为画廊才是核心」。所以根路径改发画廊，预设降成二级入口。

    `/gallery` 必须留着——QQ 里已经发出去的 `/画廊` 链接、群友的浏览器收藏
    都还指着它，删了就全是 404。
    """

    GALLERY = "画廊"
    PRESET = "大大怪 · 生图页"

    def _client(self, logged_in):
        c = friend_app.app.test_client()
        if logged_in:
            c.set_cookie("fr_sess", friend_auth.issue_cookie("2509355624"))
        return c

    def _title(self, path, logged_in):
        r = self._client(logged_in).get(path)
        self.assertEqual(r.status_code, 200, path)
        return r.get_data(as_text=True)

    def test_root_is_the_gallery_when_logged_in(self):
        html = self._title("/", True)
        self.assertIn("<title>" + self.GALLERY + "</title>", html)
        self.assertNotIn(self.PRESET, html)

    def test_root_falls_back_to_the_login_page(self):
        """没登录不能直接给画廊——它会一直在那儿转圈等着 401。"""
        html = self._title("/", False)
        self.assertIn(self.PRESET, html)

    def test_preset_page_serves_the_old_root(self):
        for logged in (True, False):
            with self.subTest(logged=logged):
                html = self._title("/preset", logged)
                self.assertIn(self.PRESET, html)

    def test_old_gallery_url_still_works(self):
        """QQ 里已经发出去的链接不能碎。"""
        html = self._title("/gallery", True)
        self.assertIn("<title>" + self.GALLERY + "</title>", html)
        html = self._title("/gallery", False)
        self.assertIn(self.PRESET, html)

    def test_gallery_links_to_the_preset_page(self):
        """右上角那个入口得指对地方，否则点一下 404。"""
        html = self._title("/", True)
        self.assertIn('href="/preset"', html)

    def test_preset_page_links_back_to_root(self):
        html = self._title("/preset", True)
        self.assertIn('href="/"', html)


class WorkshopPageTest(unittest.TestCase):
    """提示词工坊：第三方做的纯前端单文件（4.6MB），挂在 /workshop。

    两件事是这个页面的全部技术含量，漏一个手机上就用不了：

    1. **必须压缩着发**。本机是家用上行 + Cloudflare 隧道，4.6MB 原文要
       三秒多；gzip 完 600KB 上下，一秒内。所以这里钉「带了 Accept-Encoding
       就回 gzip，且解压回来一个字节不差」。
    2. **不能每次都重传**。`_secure` 默认把所有页面设成 `no-store`（共享
       设备退出登录后不该还翻得到东西），对几 MB 的单文件就是灾难。这里
       单独放行，文件到底换没换交给 ETag。
    """

    BODY = ("<!doctype html><meta charset='utf-8'><title>工坊</title>"
            + "<div class='tag'>雨夜 便利店 两个人的伞</div>" * 300)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ws_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "index.html")
        self.write(self.BODY)
        for name, val in (("WORKSHOP_HTML", self.path),):
            p = patch.object(friend_app, name, val)
            p.start()
            self.addCleanup(p.stop)
        friend_app._ws_cache.clear()
        self.addCleanup(friend_app._ws_cache.clear)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def client(self, logged_in=True):
        c = friend_app.app.test_client()
        if logged_in:
            c.set_cookie("fr_sess", friend_auth.issue_cookie("2509355624"))
        return c

    def test_logged_in_gets_the_workshop(self):
        r = self.client().get("/workshop")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/html"))
        self.assertIn("雨夜 便利店", r.get_data(as_text=True))

    def test_logged_out_gets_the_login_page(self):
        """没登录别给内容——登录页本身就是入口（它会自己 reload 回来）。"""
        r = self.client(False).get("/workshop")
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="login"', r.get_data(as_text=True))

    def test_gzip_when_the_client_asks(self):
        r = self.client().get("/workshop", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(r.headers.get("Vary"), "Accept-Encoding")
        self.assertEqual(gzip.decompress(r.data).decode("utf-8"), self.BODY)
        self.assertLess(len(r.data), len(self.BODY.encode("utf-8")) / 2,
                        "压不动就说明 gzip 根本没生效")

    def test_can_be_cached_but_not_forever(self):
        r = self.client().get("/workshop")
        cc = r.headers.get("Cache-Control")
        self.assertIn("private", cc)
        self.assertNotIn("no-store", cc, "no-store 等于每次刷新重传几 MB")
        self.assertIn("max-age=600", cc)
        self.assertIn("ETag", r.headers)

    def test_unchanged_file_is_a_304(self):
        c = self.client()
        etag = c.get("/workshop").headers["ETag"]
        r = c.get("/workshop", headers={"If-None-Match": etag})
        self.assertEqual(r.status_code, 304)
        self.assertEqual(r.data, b"", "304 不该再带一遍正文")
        # gzip 是另一种字节，自己有 ETag，也要能单独 304
        gz = c.get("/workshop", headers={"Accept-Encoding": "gzip"})
        r = c.get("/workshop", headers={"Accept-Encoding": "gzip",
                                        "If-None-Match": gz.headers["ETag"]})
        self.assertEqual(r.status_code, 304)

    def test_content_type_is_not_doubled(self):
        """`text/html; charset=utf-8; charset=utf-8` 这种重复是 Flask 又补了一次。"""
        r = self.client().get("/workshop")
        self.assertNotIn("charset=utf-8; charset", r.headers["Content-Type"])

    def test_new_file_breaks_the_cache(self):
        """换了版本（1.3.5 → 1.3.6）页面必须跟着变，不能被浏览器缓存糊住。"""
        c = self.client()
        old = c.get("/workshop").headers["ETag"]
        self.write(self.BODY + "<p>1.3.6</p>")
        os.utime(self.path, (time.time() + 5, time.time() + 5))
        r = c.get("/workshop", headers={"If-None-Match": old})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.headers["ETag"], old)
        self.assertIn("1.3.6", r.get_data(as_text=True))

    def test_missing_configuration_says_so_without_leaking_paths(self):
        with patch.object(friend_app, "WORKSHOP_HTML", ""):
            r = self.client().get("/workshop")
        self.assertEqual(r.status_code, 404)
        body = r.get_data(as_text=True)
        self.assertIn("还没配上", body)
        self.assertNotIn("D:\\", body, "别把本机路径吐给群友")

    def test_vanished_file_is_404_not_500(self):
        os.remove(self.path)
        r = self.client().get("/workshop")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
