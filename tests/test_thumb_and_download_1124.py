"""2026-11-24：出图走缩略图 + 下载走浏览器缓存 + 网关透传缓存头。

用户报的两条：
  1. 「图片生成之后，要加载非常的慢」——出图原图 2~8MB PNG，聊天区铺十几张
     就是几十 MB 全从家用上行出。
  2. 「图片已经加载完成了，点下载竟然是从服务器下载的，而不是直接拿已经
     缓存的图片」——`<a download>` 触发的是导航式下载，不查 HTTP 缓存。

这一组只钉「接口事实」，不钉实现细节：
  · `/api/gen/img?w=` 认白名单宽度并发缩略图；不认的值退回原图
  · 缩略图比原图小两个数量级
  · 原图和缩略图都带 immutable 长缓存 + ETag，带 If-None-Match 能拿 304
  · 5175 的非流式转发分支也把 Cache-Control / ETag 抄过来（原来一个都不带）
"""
import io
import os
import unittest
from unittest import mock

from app import main
from app.features.image import routes as gen_api


# 挑一张确实存在于 ComfyUI output 里的英文路径图；没有就整体 skip。
def _pick_image():
    try:
        from app.config import COMFYUI_OUTPUT_DIR as D
    except Exception:                                   # noqa: BLE001
        return None
    if not D or not os.path.isdir(D):
        return None
    for root, _dirs, files in os.walk(D):
        for f in files:
            if not f.lower().endswith(".png"):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, D).replace("\\", "/")
            if all(ord(c) < 128 for c in rel) and os.path.getsize(full) > 1024:
                return rel
    return None


class ThumbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = main.app.test_client()
        cls.rel = _pick_image()

    def setUp(self):
        if not self.rel:
            self.skipTest("本机没有可用的演示图（ComfyUI output 目录为空）")

    def _get(self, query="", headers=None):
        return self.client.get("/api/gen/img?f=" + self.rel + query,
                               headers=headers or {})

    def test_whitelist_widths_accepted(self):
        """240/480/640/900 都认，返回的是缩略图（比原图小得多）。"""
        base = self._get("")
        self.assertEqual(base.status_code, 200)
        full_len = len(base.data)
        for w in gen_api._IMG_THUMB_W:
            r = self._get("&w=%d" % w)
            self.assertEqual(r.status_code, 200, "w=%d 没出来" % w)
            self.assertLess(len(r.data), full_len,
                            "w=%d 竟然不比原图小" % w)
            self.assertIn("image/", r.headers.get("Content-Type", ""))

    def test_thumb_is_much_smaller(self):
        """640 的缩略图至少比原图小 20 倍——这条就是用户说的「慢」的解药。"""
        base = self._get("")
        r = self._get("&w=640")
        self.assertGreater(len(base.data) / max(1, len(r.data)), 20,
                           "缩略图没有明显变小，优化没生效")

    def test_unknown_width_falls_back_to_original(self):
        """白名单外的值当没传：退回原图，不是 400 也不是空。"""
        base = self._get("")
        for bad in ("999", "1000", "abc", "-1", "0"):
            r = self._get("&w=" + bad)
            self.assertEqual(r.status_code, 200, "w=%s 不该报错" % bad)
            self.assertEqual(len(r.data), len(base.data),
                             "w=%s 竟然没退回原图" % bad)

    def test_thumb_and_original_both_immutable(self):
        """两张都带一年 immutable：浏览器才不会每次重下。"""
        for q in ("", "&w=640"):
            h = self._get(q).headers.get("Cache-Control", "")
            self.assertIn("immutable", h, "q=%s 丢了 immutable" % (q or "原图"))
            self.assertIn("max-age=31536000", h)
            self.assertIn("private", h)   # 私图不能进共享缓存

    def test_thumb_supports_304(self):
        """带 If-None-Match 再取缩略图 → 304，一个字节都不传。"""
        r1 = self._get("&w=640")
        etag = r1.headers.get("ETag")
        self.assertTrue(etag, "缩略图没 ETag，304 无从谈起")
        r2 = self._get("&w=640", headers={"If-None-Match": etag})
        self.assertEqual(r2.status_code, 304)
        self.assertEqual(len(r2.data), 0)

    def test_thumb_reuses_gallery_implementation(self):
        """不许自己再造一套缩略图：必须走 gallery.thumb（WebP + 锁 + 缓存）。"""
        src = io.open(gen_api.__file__, encoding="utf-8").read()
        self.assertIn("_gallery.thumb", src,
                      "又自己写了一份缩略图逻辑，应该复用 gallery.thumb")


class GatewayHeaderTest(unittest.TestCase):
    """5175 的非流式转发分支必须把缓存头抄过来（原来一个都不带）。"""

    def test_forward_copies_cache_headers(self):
        import friend_app
        src = io.open(friend_app.__file__, encoding="utf-8").read()
        # 非流式那一段（Response(r.content, ...) 之后）要照抄这几个头
        i = src.index("Response(r.content, status=r.status_code")
        tail = src[i:i + 1200]
        for h in ("Cache-Control", "ETag", "Last-Modified"):
            self.assertIn('"%s"' % h, tail,
                          "非流式分支没透传 %s" % h)

    def test_gen_img_is_streamed(self):
        """/api/gen/img 要在流式白名单里：否则整张图先读进内存、没有进度。"""
        import friend_app
        src = io.open(friend_app.__file__, encoding="utf-8").read()
        i = src.index("streaming = (request.method == \"GET\"")
        j = src.index("#  ↑ moss 漫画图单张", i)
        self.assertIn("/api/gen/img", src[i:j],
                      "/api/gen/img 还是非流式转发")


class FrontendDownloadTest(unittest.TestCase):
    """前端：缩略图地址 + 下载走 fetch（不再裸 <a download href>）。"""

    def _read(self, *parts):
        base = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", *parts)
        return io.open(base, encoding="utf-8").read()

    def test_chat_uses_thumb_width(self):
        s = self._read("workspace", "js", "chat.js")
        self.assertIn("WSGen.img(job.file, 640)", s,
                      "聊天区的图没走缩略图")
        self.assertIn("dataset.full", s, "没留原图地址，点开升级不了")

    def test_archive_uses_thumb_width(self):
        s = self._read("workspace", "js", "archive.js")
        self.assertIn("WSGen.img(m.file, 640)", s,
                      "历史重放还在拉原图")

    def test_download_goes_through_fetch(self):
        s = self._read("workspace", "js", "chat.js")
        i = s.index("function downloadImage")
        body = s[i:i + 1600]
        self.assertIn("fetch(", body, "下载没先走 fetch，还是会重下一遍")
        self.assertIn('cache: "default"', body,
                      "没指定 cache: default，可能绕过缓存")

    def test_gen_img_supports_width_param(self):
        s = self._read("workspace", "js", "gen.js")
        self.assertIn("function img(file, w)", s)
        self.assertIn('"&w="', s)


if __name__ == "__main__":
    unittest.main()
