# -*- coding: utf-8 -*-
"""5175 转发层：大图必须流式、Range 必须透传、qq 不能被客户端改。

这几条是「下载原图」的地基。转发一旦退回 `r.content`，一张 26MB 的图会在
5175 里多占一份内存，几个人同时下就把这个进程顶爆；Range 不转发则断点续传
和下载器分段全部退化成整包重传。
"""

import os
import sys
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


if __name__ == "__main__":
    unittest.main()
