"""2026-11-24 第二轮：512KB 档 / 下载秒出 / 排队卡死重试 / 空分类隐藏 / 画同款不再识图。

用户这一天提的六条里，能钉成接口事实的五条：

  1. 「有办法使用工作台的话，图片压缩到 512k 吗？下载就默认下这个 512k 的」
     → `/api/gen/img?fit=512k`、`/api/gal/img/<id>?fit=512k`；前端列表和下载
       都用这一档。
  2. 「现在工作台依旧显示昨天晚上的图还是排队中，卡死了，超时功能没有上」
     → 前端按时间判超时并给「重试」；服务端把「线程没了却还是 queued」收成
       error 并把 `stuck` 下发。
  3. 「清空 web 的时候文件夹目录是空的了，这个标签就应该隐藏」
     → 分类条只列目录还在的分类；当前分类没了给 `grp_gone`。
  4. 「画同款……发送的时候再请求一次大大怪的吗？」
     → 前端带了 `ref_described`，后端就**不再**识图。
  5. 「点下载要等七八秒」「每次重进图片不是从缓存拿的」
     → 下载走压缩档 + 本地缓存（IndexedDB，key 不含域名）；取图多一个
       站点签名 `k=`，换隧道域名后缓存照命中。

这里只钉「接口和前端行为的事实」，不钉实现细节。
"""
import io
import os
import re
import unittest
from unittest import mock

from app import main
from app.features.gallery import routes as gallery_api
from app.features.image import routes as gen_api


def _pick_image():
    """挑一张确实存在的英文路径原图；没有就整组 skip。"""
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


def _src(*parts):
    base = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), *parts)
    return io.open(base, encoding="utf-8").read()


class FitTest(unittest.TestCase):
    """`fit=512k`：长边从小往大试，取第一张不超过 512KB 的。"""

    @classmethod
    def setUpClass(cls):
        cls.client = main.app.test_client()
        cls.rel = _pick_image()
        # `/api/gen/img` 要过内部令牌（5175 转发时带的那一个）
        from app.gateways import friend_auth
        cls.h = {"X-Friend-Token": friend_auth.secret()}
        cls.base_url = "http://localhost:5174"

    def setUp(self):
        if not self.rel:
            self.skipTest("本机没有可用的演示图")

    def _get(self, q, headers=None):
        return self.client.get("/api/gen/img?f=" + self.rel + q,
                               headers=headers or self.h,
                               base_url=self.base_url)

    def test_1m_is_under_cap(self):
        r = self._get("&fit=1m")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(len(r.data), gen_api._IMG_FIT_CAP,
                            "1M 档超过了上限")
        self.assertEqual(r.headers.get("Content-Type"), "image/webp")

    def test_cap_is_one_megabyte(self):
        """2026-11-24 用户：「现在只有 102k 还是太小了……压到 1m 即可」。"""
        self.assertEqual(gen_api._IMG_FIT_CAP, 1024 * 1024)

    def test_fit_tries_large_widths_first(self):
        """从大往小试。反过来（768 起）第一张往往才 50KB 就「合格」，
        永远出不来高清图——上一版「只有 102k」就是这么来的。"""
        self.assertEqual(tuple(gen_api._IMG_FIT_W),
                         tuple(sorted(gen_api._IMG_FIT_W, reverse=True)))
        self.assertGreaterEqual(gen_api._IMG_FIT_W[0], 1536)

    def test_1m_bigger_than_grid_thumb_smaller_than_original(self):
        """它得比 640 缩略图清楚、又比原图小得多。"""
        full = len(self._get("").data)
        grid = len(self._get("&w=640").data)
        fit = len(self._get("&fit=1m").data)
        self.assertGreater(fit, grid, "1M 档比网格缩略图还小，白做")
        self.assertLess(fit * 4, full, "1M 档没比原图小多少")

    def test_1m_supports_304(self):
        r1 = self._get("&fit=1m")
        h = dict(self.h)
        h["If-None-Match"] = r1.headers.get("ETag")
        r2 = self._get("&fit=1m", h)
        self.assertEqual(r2.status_code, 304)
        self.assertEqual(len(r2.data), 0)

    def test_legacy_512k_still_served(self):
        """老前端 / 存着的旧链接发 `512k`，按同一个上限发，别碎图。"""
        r = self._get("&fit=512k")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(len(r.data), gen_api._IMG_FIT_CAP)

    def test_unknown_fit_falls_back(self):
        base = len(self._get("").data)
        r = self._get("&fit=999k")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), base, "不认识的 fit 该退回原图")

    def test_reuses_gallery_cache(self):
        """不许另造一套缩略图：必须和 gallery.thumb 共用缓存目录与锁。"""
        src = io.open(gen_api.__file__, encoding="utf-8").read()
        self.assertIn("_gallery.thumb", src)
        self.assertIn("_IMG_FIT_W", src)


class ImgSignTest(unittest.TestCase):
    """站点签名：本地缓存取字节时带不了 cookie，靠 `k=` 过校验。"""

    @classmethod
    def setUpClass(cls):
        cls.client = main.app.test_client()
        cls.rel = _pick_image()

    def setUp(self):
        if not self.rel:
            self.skipTest("本机没有可用的演示图")

    def test_imgsign_issues_token(self):
        d = self.client.get("/api/gen/imgsign").get_json()
        self.assertTrue(d and d.get("ok"))
        self.assertGreaterEqual(len(d.get("token") or ""), 16)

    def test_token_alone_is_enough(self):
        """不带内部令牌、只带 k= → 仍然给图（这就是 no-cors 那条路）。"""
        tok = self.client.get("/api/gen/imgsign").get_json()["token"]
        r = self.client.get(
            "/api/gen/img?f=%s&fit=512k&k=%s" % (self.rel, tok),
            base_url="http://localhost:5174")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.data), 0)

    def test_bad_token_without_internal_key_is_403(self):
        """坏签名 + 没内部令牌 → 403。

        `base_url` 固定成 127.0.0.1:5174，让 Gallery 的「本机直连免检」
        生效——不然这条用例会随机器环境（局域网 IP / 端口）时绿时红。
        """
        from app.features.gallery import routes as gal
        with mock.patch.object(gal, "_local_trusted", lambda: False), \
             mock.patch("app.gateways.friend_auth.secret", lambda: "tok"):
            r = self.client.get(
                "/api/gen/img?f=%s&fit=512k&k=nope" % self.rel,
                base_url="http://127.0.0.1:5174")
        self.assertEqual(r.status_code, 403, "坏签名竟然放过去了")


class GatewayGenImgTest(unittest.TestCase):
    """5175：带签名的取图（登录态之外）要放行；imgsign 也要放行。"""

    def test_gateway_allows_signed_img(self):
        src = io.open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "friend_app.py"), encoding="utf-8").read()
        i = src.index('def proxy_gen(sub):')
        body = src[i:i + 1800]
        self.assertIn('sub == "imgsign"', body)
        self.assertIn('sub == "img"', body, "带签名的取图没放行")
        self.assertIn('request.args.get("k")', body)


class StuckJobTest(unittest.TestCase):
    """服务端兜底：线程没了却还 queued 的任务要收成 error 并带 stuck。"""

    def test_reap_marks_dead_thread_job(self):
        c = main.app.test_client()
        with gen_api._JOBS_LOCK:
            gen_api._JOBS.clear()
            gen_api._JOBS["GP-TEST-000000-000"] = {
                "id": "GP-TEST-000000-000", "skill": "silver", "prompt": "x",
                "seed": 1, "state": "queued", "error": "", "file": "",
                "ts": 0, "_thread_alive": False, "abort": None,
            }
        try:
            d = c.get("/api/gen/status").get_json()
            job = [j for j in d["jobs"] if j["id"] == "GP-TEST-000000-000"][0]
            self.assertEqual(job["state"], "error")
            self.assertTrue(job["stuck"], "没标 stuck，前端就不知道该给重试")
        finally:
            with gen_api._JOBS_LOCK:
                gen_api._JOBS.clear()

    def test_alive_job_not_reaped(self):
        """线程还活着就不许动它：画得慢不等于死了。"""
        c = main.app.test_client()
        with gen_api._JOBS_LOCK:
            gen_api._JOBS.clear()
            gen_api._JOBS["GP-TEST-LIVE-000"] = {
                "id": "GP-TEST-LIVE-000", "skill": "silver", "prompt": "x",
                "seed": 1, "state": "queued", "error": "", "file": "",
                "ts": 0, "_thread_alive": True, "abort": mock.Mock(),
            }
        try:
            d = c.get("/api/gen/status").get_json()
            job = [j for j in d["jobs"] if j["id"] == "GP-TEST-LIVE-000"][0]
            self.assertEqual(job["state"], "queued")
        finally:
            with gen_api._JOBS_LOCK:
                gen_api._JOBS.clear()


class RefDescribedTest(unittest.TestCase):
    """`ref_described` → 后端不再识图（也不再拼第二遍描述）。"""

    def test_described_skips_describe(self):
        src = io.open(gen_api.__file__, encoding="utf-8").read()
        i = src.index('ref_ids, ref_note = _ref_ids(')
        tail = src[i:i + 900]
        self.assertIn("ref_described", tail,
                      "后端没认 ref_described，画同款会识两次图")
        self.assertIn("ref_ids = []", tail)


class GalleryGroupTest(unittest.TestCase):
    """分类条：目录不在的分类不列；当前分类没了给 grp_gone。"""

    def _app(self):
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(gallery_api.bp)
        return app

    def test_dir_alive(self):
        self.assertTrue(gallery_api._dir_alive(""))
        self.assertTrue(gallery_api._dir_alive(os.getcwd()))
        self.assertFalse(gallery_api._dir_alive("Z:/definitely/not/here"))

    def test_grp_gone_flag_in_payload(self):
        from app.features.gallery import gallery
        gallery.init()
        app = self._app()
        c = app.test_client()
        h = {"X-Friend-Token": "tok"}
        with mock.patch("app.gateways.friend_auth.secret", lambda: "tok"), \
             mock.patch.object(gallery_api, "GALLERY_OWNER_QQ", "10001"):
            with gallery.connect() as con:
                con.execute("DELETE FROM works")
                con.execute(
                    "INSERT INTO works(key,src,owner,grp,title,path,n,sort,ord,ts,seen)"
                    " VALUES('k1','owner','','web','空目录','/tmp/x',5,1,'',0,0)")
            d = c.get("/api/gal/list?qq=10001&scope=owner&grp=web",
                      headers=h).get_json()
            self.assertTrue(d.get("ok"))
            self.assertFalse(d.get("grp_gone"), "分类还在却报了 gone")
            d2 = c.get("/api/gal/list?qq=10001&scope=owner&grp=不存在",
                       headers=h).get_json()
            self.assertTrue(d2.get("grp_gone"), "分类没了该报 grp_gone")


class FrontendTest(unittest.TestCase):
    """前端事实：档位一致、超时给重试、下载走本地缓存、重放先问服务端。"""

    def test_chat_uses_fit_1m(self):
        s = _src("web", "workspace", "js", "chat.js")
        self.assertIn('var FIT = "1m"', s)
        self.assertIn("WSGen.img(file, FIT)", s, "列表/下载没走压缩档")

    def test_click_does_not_fetch_original(self):
        """2026-11-24 用户：「点击图片不需要重新传 10m 的原图」。
        所以点开只能是放大这一张，不许再挂一个不带 fit 的地址。"""
        s = _src("web", "workspace", "js", "chat.js")
        self.assertNotIn("dataset.full", s, "还在点开时换原图地址")
        self.assertIn("function toggleFull", s)

    def test_archive_uses_same_fit(self):
        s = _src("web", "workspace", "js", "archive.js")
        self.assertIn('var FIT = "1m"', s)
        self.assertIn("global.WSChat.shotEl(m.file)", s,
                      "重放没复用同一张（地址不同就多下一遍）")

    def test_download_hits_local_cache_first(self):
        s = _src("web", "workspace", "js", "chat.js")
        i = s.index("function downloadImage")
        body = s[i:i + 1800]
        self.assertIn("WSImg.get", body, "下载没先问本地缓存")
        self.assertIn("saveBlob", body)

    def test_timeout_gives_retry(self):
        s = _src("web", "workspace", "js", "chat.js")
        self.assertIn("SETTLE_MS", s)
        self.assertIn("PAINT_MS", s)
        self.assertIn("checkTimeout", s)
        self.assertIn('"重试"', s)

    def test_restore_asks_status_before_polling(self):
        s = _src("web", "workspace", "js", "archive.js")
        i = s.index("again.length")
        self.assertIn("WSGen.status()", s[i:], "重放没先问服务端状态")
        self.assertIn("failCard", s[i:])

    def test_imgcache_key_has_no_origin(self):
        s = _src("web", "workspace", "js", "imgcache.js")
        self.assertNotIn("location.origin", s,
                         "缓存 key 里带了域名，换隧道就失效")
        self.assertIn("indexedDB", s)
        self.assertIn("300 * 1024 * 1024", s, "容量上限不是 300MB")

    def test_cache_key_drops_signature(self):
        """缓存 key 不能带 `k=`：签名每次会话可能换，带着它缓存会成孤儿。"""
        g = _src("web", "workspace", "js", "gen.js")
        self.assertIn("function imgKey(file, fit)", g)
        i = g.index("function imgKey")
        seg = g[i:].split("\n  }", 1)[0]        # 只看这个函数体
        self.assertNotIn("SIGN", seg, "imgKey 里不该掺签名")
        c = _src("web", "workspace", "js", "chat.js")
        self.assertIn("imgKey(file, FIT)", c, "下载没按不带签名的 key 查缓存")

    def test_chat_html_loads_imgcache(self):
        s = _src("web", "workspace", "chat.html")
        self.assertIn("js/imgcache.js", s)

    def test_gallery_hides_tab_via_grp_gone(self):
        s = _src("web", "workspace", "js", "gallery.js")
        self.assertIn("grp_gone", s)


if __name__ == "__main__":
    unittest.main()
