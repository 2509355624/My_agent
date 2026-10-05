"""NovelAI 客户端 + 开关判定测试。

零真实网络：requests.Session 用 mock 替掉，HTTP 一行都不发；winreg 也 mock
掉（没开代理 / 非 Windows 时读不到，不影响判定）。
"""

import base64
import io
import unittest
import zipfile
from unittest import mock

from PIL import Image

from app import agents
from app import nai


class GenerateTest(unittest.TestCase):
    """nai.generate：请求体 / 鉴权 / zip 解包。"""

    def _fake_session(self, status=200, content_type="application/zip", body=None):
        # 默认造一个真 zip（image_0.png），避免 generate() 解包时 BadZipFile。
        if body is None:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("image_0.png", b"PNG-BYTES")
            body = buf.getvalue()
        resp = mock.Mock()
        resp.status_code = status
        resp.headers = {"Content-Type": content_type}
        resp.content = body
        resp.raise_for_status = lambda: None
        sess = mock.Mock()
        sess.post = mock.Mock(return_value=resp)
        return sess

    def _run(self, sess, prompt="a cat", proxies="", key="pst-test", wide=False):
        with mock.patch.object(nai, "requests") as req, \
                mock.patch.object(nai, "NAI_PROXY", proxies), \
                mock.patch.object(nai, "NAI_API_KEY", key):
            req.Session = mock.Mock(return_value=sess)
            return nai.generate(prompt, wide=wide)

    def test_builds_v5_request_and_unzips(self):
        # 造一个真 zip，里面 image_0.png
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("image_0.png", b"PNG-BYTES")
        sess = self._fake_session(body=buf.getvalue())
        out = self._run(sess)
        self.assertEqual(out, b"PNG-BYTES")
        args, kwargs = sess.post.call_args
        self.assertEqual(args[0], nai.NAI_ENDPOINT)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer pst-test")
        body = kwargs["json"]
        self.assertEqual(body["model"], "nai-diffusion-5-full")
        # V5 的硬性结构：params_version=3 + 两条 caption
        self.assertEqual(body["parameters"]["params_version"], 3)
        self.assertIn("v4_prompt", body["parameters"])
        self.assertIn("v4_negative_prompt", body["parameters"])
        self.assertEqual(body["parameters"]["width"], nai.NAI_WIDTH)
        self.assertEqual(body["parameters"]["height"], nai.NAI_HEIGHT)
        self.assertEqual(body["parameters"]["prompt"], "a cat")
        self.assertEqual(body["input"], "a cat")

    def test_missing_key_raises(self):
        sess = self._fake_session()
        with self.assertRaises(RuntimeError):
            self._run(sess, key="")

    def test_wide_uses_landscape_size(self):
        """`nai_wide` 渠道：wide=True → 横版 1216×832（竖版转 90°，像素数一样）。"""
        sess = self._fake_session()
        self._run(sess, wide=True)
        _, kwargs = sess.post.call_args
        p = kwargs["json"]["parameters"]
        self.assertEqual((p["width"], p["height"]),
                         (nai.NAI_WIDE_WIDTH, nai.NAI_WIDE_HEIGHT))
        self.assertEqual((p["width"], p["height"]), (1216, 832))
        # 横版必须真的比竖版宽，别把两个常量写反
        self.assertGreater(nai.NAI_WIDE_WIDTH, nai.NAI_WIDE_HEIGHT)

    def test_unzips_when_content_type_is_octet_stream(self):
        # NAI 真实返回 binary/octet-stream（不是 application/zip）。靠 magic 字节
        # 识别 zip（PK\x03\x04），不靠 ctype——否则 zip 被当 png 发出去，群里收坏图。
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("image_0.png", b"PNG-REAL")
        sess = self._fake_session(content_type="binary/octet-stream",
                                   body=buf.getvalue())
        out = self._run(sess)
        self.assertEqual(out, b"PNG-REAL")

    def test_returns_raw_png_when_not_zip(self):
        # 极少数情况下 NAI 直接给 png 字节（不是 zip），原样返回。
        sess = self._fake_session(content_type="image/png",
                                   body=b"\x89PNG\r\n\x1a\nRAW")
        out = self._run(sess)
        self.assertEqual(out, b"\x89PNG\r\n\x1a\nRAW")

    def test_empty_prompt_raises(self):
        sess = self._fake_session()
        with self.assertRaises(ValueError):
            self._run(sess, prompt="   ")

    def test_uses_explicit_proxy(self):
        sess = self._fake_session()
        self._run(sess, proxies="http://127.0.0.1:65532")
        self.assertEqual(sess.proxies, {
            "http": "http://127.0.0.1:65532", "https": "http://127.0.0.1:65532"})

    def test_registry_proxy_autodetect(self):
        # 没显式配 → 读注册表；模拟用户开了 VPN，ProxyServer 是分协议写法
        sess = self._fake_session()
        reg = mock.Mock()
        reg.QueryValueEx = lambda k, n: (1, "1") if n == "ProxyEnable" else (
            "http=127.0.0.1:65532;https=127.0.0.1:65532", 0)
        wr = mock.Mock(OpenKey=mock.Mock(return_value=reg),
                       HKEY_CURRENT_USER=1, QueryValueEx=reg.QueryValueEx)
        with mock.patch.object(nai, "NAI_PROXY", ""), \
                mock.patch.object(nai, "NAI_API_KEY", "pst-test"), \
                mock.patch.object(nai, "winreg", wr), \
                mock.patch.object(nai, "requests") as req:
            req.Session = mock.Mock(return_value=sess)
            nai.generate("a cat")
        self.assertEqual(sess.proxies, {
            "http": "127.0.0.1:65532", "https": "127.0.0.1:65532"})

    def test_registry_proxy_plain_address(self):
        # 注册表只写了个裸地址（不开分协议）也能用
        sess = self._fake_session()
        reg = mock.Mock()
        reg.QueryValueEx = lambda k, n: (1, "1") if n == "ProxyEnable" else (
            "127.0.0.1:8888", 0)
        wr = mock.Mock(OpenKey=mock.Mock(return_value=reg),
                       HKEY_CURRENT_USER=1, QueryValueEx=reg.QueryValueEx)
        with mock.patch.object(nai, "NAI_PROXY", ""), \
                mock.patch.object(nai, "NAI_API_KEY", "pst-test"), \
                mock.patch.object(nai, "winreg", wr), \
                mock.patch.object(nai, "requests") as req:
            req.Session = mock.Mock(return_value=sess)
            nai.generate("a cat")
        self.assertEqual(sess.proxies, {
            "http": "127.0.0.1:8888", "https": "127.0.0.1:8888"})


class ProxyDetectTest(unittest.TestCase):
    """_nai_proxies：显式 / 注册表 / 关闭 三态。"""

    def test_explicit_proxy(self):
        with mock.patch.object(nai, "NAI_PROXY", "http://x:1"):
            self.assertEqual(nai._nai_proxies(), {
                "http": "http://x:1", "https": "http://x:1"})

    def test_no_proxy_and_no_registry(self):
        with mock.patch.object(nai, "NAI_PROXY", ""), \
                mock.patch.object(nai, "winreg", None):
            self.assertIsNone(nai._nai_proxies())

    def test_registry_no_server(self):
        # 注册表压根没写 ProxyServer（真·没配代理）→ 返回 None，直连。
        reg = mock.Mock()
        reg.QueryValueEx = lambda k, n: ("", 1) if n == "ProxyServer" else (
            0, "0")
        wr = mock.Mock(OpenKey=mock.Mock(return_value=reg),
                       HKEY_CURRENT_USER=1, QueryValueEx=reg.QueryValueEx)
        with mock.patch.object(nai, "NAI_PROXY", ""), \
                mock.patch.object(nai, "winreg", wr):
            self.assertIsNone(nai._nai_proxies())

    def test_registry_server_without_enable(self):
        # 用户 VPN（nano）的常见状态：ProxyServer 写好了但 ProxyEnable=0
        # （走虚拟网卡路由）。NAI 是外网，必须借这个本地代理出网，所以
        # 只要 ProxyServer 非空就用它，不卡 ProxyEnable。
        reg = mock.Mock()
        reg.QueryValueEx = (
            lambda k, n: ("http=127.0.0.1:65532;https=127.0.0.1:65532", 1)
            if n == "ProxyServer" else (0, "0"))
        wr = mock.Mock(OpenKey=mock.Mock(return_value=reg),
                       HKEY_CURRENT_USER=1, QueryValueEx=reg.QueryValueEx)
        with mock.patch.object(nai, "NAI_PROXY", ""), \
                mock.patch.object(nai, "winreg", wr):
            self.assertEqual(nai._nai_proxies(), {
                "http": "127.0.0.1:65532", "https": "127.0.0.1:65532"})


class Img2ImgTest(unittest.TestCase):
    """nai.generate_img2img：action=img2img、纯 base64 源图、strength 透传。"""

    def _fake_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("image_0.png", b"PNG-I2I")
        return buf.getvalue()

    def _run(self, prompt="make it night", image="QUJD", strength=None,
             size=None):
        sess = GenerateTest._fake_session(self, body=self._fake_zip())
        with mock.patch.object(nai, "requests") as req, \
                mock.patch.object(nai, "NAI_PROXY", ""), \
                mock.patch.object(nai, "NAI_API_KEY", "pst-test"):
            req.Session = mock.Mock(return_value=sess)
            extra = {}
            if strength is not None:
                extra["strength"] = strength
            if size is not None:
                extra["width"], extra["height"] = size
            nai.generate_img2img(prompt, image, **extra)
        args, kwargs = sess.post.call_args
        return kwargs["json"]

    def test_carries_the_size_computed_for_the_source(self):
        """尺寸原样透传：prepare_image 算出来多大，NAI 就按多大出图。"""
        p = self._run(size=(512, 1536))["parameters"]
        self.assertEqual((p["width"], p["height"]), (512, 1536))

    def test_defaults_to_portrait_when_no_size_given(self):
        # 兜底：老快照（没有 width/height）退回竖版，不会拿 None 去撞 NAI
        p = self._run()["parameters"]
        self.assertEqual((p["width"], p["height"]),
                         (nai.NAI_WIDTH, nai.NAI_HEIGHT))

    def test_action_and_image_and_strength(self):
        body = self._run(strength=0.35)
        self.assertEqual(body["action"], "img2img")
        self.assertEqual(body["parameters"]["image"], "QUJD")
        self.assertEqual(body["parameters"]["strength"], 0.35)
        self.assertEqual(body["parameters"]["noise"], 0.0)
        # V5 结构与文生图一致（caption 两条照带）
        self.assertEqual(body["parameters"]["params_version"], 3)
        self.assertIn("v4_prompt", body["parameters"])

    def test_default_strength(self):
        body = self._run()
        self.assertEqual(body["parameters"]["strength"], nai.NAI_I2I_STRENGTH)

    def test_blank_image_raises(self):
        with self.assertRaises(ValueError):
            nai.generate_img2img("x", "  ")

    def test_empty_prompt_raises(self):
        with self.assertRaises(ValueError):
            nai.generate_img2img("   ", "QUJD")


class FitSizeTest(unittest.TestCase):
    """nai.fit_size：源图比例 → NAI 能用的尺寸（64 的倍数、总像素≈竖版基准）。"""

    def test_always_multiple_of_64_and_within_bounds(self):
        for w, h in [(4000, 3000), (2000, 500), (300, 1600), (500, 500),
                     (12345, 678), (1920, 1080), (37, 91), (64, 64)]:
            tw, th = nai.fit_size(w, h)
            self.assertEqual(tw % nai._NAI_SIZE_STEP, 0, (w, h))
            self.assertEqual(th % nai._NAI_SIZE_STEP, 0, (w, h))
            for side in (tw, th):
                self.assertGreaterEqual(side, nai._NAI_SIZE_MIN, (w, h))
                self.assertLessEqual(side, nai._NAI_SIZE_MAX, (w, h))

    def test_landscape_stays_landscape_and_portrait_stays_portrait(self):
        tw, th = nai.fit_size(1920, 1080)
        self.assertGreater(tw, th)
        tw, th = nai.fit_size(1080, 1920)
        self.assertGreater(th, tw)
        # 极端宽（8:1）会被上限夹住，但方向不能翻
        tw, th = nai.fit_size(4000, 500)
        self.assertGreater(tw, th)

    def test_baseline_ratios_round_trip(self):
        self.assertEqual(nai.fit_size(832, 1216), (832, 1216))
        self.assertEqual(nai.fit_size(1216, 832), (1216, 832))
        self.assertEqual(nai.fit_size(1024, 1024), (1024, 1024))

    def test_ratio_is_kept_for_ordinary_photos(self):
        for w, h in [(1200, 1600), (1600, 1200), (3000, 2000), (800, 800)]:
            tw, th = nai.fit_size(w, h)
            self.assertAlmostEqual(tw / float(th), w / float(h), delta=0.06,
                                   msg=(w, h, tw, th))

    def test_unusable_size_falls_back_to_portrait(self):
        for bad in [(0, 0), (None, None), ("x", "y"), (-5, 10)]:
            self.assertEqual(nai.fit_size(*bad),
                             (nai.NAI_WIDTH, nai.NAI_HEIGHT), bad)


class PrepareImageTest(unittest.TestCase):
    """nai.prepare_image：按**源图比例**折算尺寸 + 纯 base64。

    2026-10-03 起不再一律裁成竖版——垫图是「改造这张」，构图该跟原图一致；
    裁剪只剩「消掉 64 倍数取整那点偏差」的量。
    """

    def _png(self, w, h, mode="RGB"):
        buf = io.BytesIO()
        Image.new(mode, (w, h), (200, 120, 90)).save(buf, "PNG")
        return buf.getvalue()

    def _decode(self, prepared):
        b64, w, h = prepared
        self.assertNotIn(b"://", base64.b64decode(b64)[:64])  # 纯 base64，无 data: 前缀
        im = Image.open(io.BytesIO(base64.b64decode(b64)))
        # 报出来的尺寸必须跟真图一致——worker 拿它当 NAI 的 width/height，
        # 不一致就等于「垫一张 A 尺寸的图、要一张 B 尺寸的图」。
        self.assertEqual(im.size, (w, h))
        return im

    def test_landscape_source_stays_landscape(self):
        im = self._decode(nai.prepare_image(self._png(1920, 1080)))
        self.assertGreater(im.size[0], im.size[1])

    def test_portrait_source_stays_portrait(self):
        im = self._decode(nai.prepare_image(self._png(1080, 1920)))
        self.assertGreater(im.size[1], im.size[0])

    def test_source_ratio_is_kept(self):
        im = self._decode(nai.prepare_image(self._png(1200, 1600)))   # 3:4
        self.assertAlmostEqual(im.size[0] / float(im.size[1]), 0.75,
                               delta=0.06)

    def test_alpha_is_flattened(self):
        im = self._decode(nai.prepare_image(self._png(64, 64, "RGBA")))
        self.assertEqual(im.mode, "RGB")

    def test_garbage_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            nai.prepare_image(b"definitely not an image")


class AllowedTest(unittest.TestCase):
    """agents.nai_allowed 的三层闸（.env 总闸 → settings 全局 → 单群白名单）。"""

    def _settings(self, **kw):
        return kw

    def test_master_off_refuses(self):
        with mock.patch.object(agents, "NAI_ENABLED", False):
            ok, _ = agents.nai_allowed("qq", "group", "9")
        self.assertFalse(ok)

    def test_global_off_refuses(self):
        with mock.patch.object(agents, "NAI_ENABLED", True), \
                mock.patch.object(agents, "load_settings",
                                 return_value=self._settings()):
            ok, why = agents.nai_allowed("qq", "group", "9")
        self.assertFalse(ok)
        self.assertIn("未在本", why)

    def test_group_not_in_whitelist_refuses(self):
        with mock.patch.object(agents, "NAI_ENABLED", True), \
                mock.patch.object(agents, "load_settings", return_value=self._settings(
                    nai_enabled=True, nai_groups=["1", "2"])):
            ok, _ = agents.nai_allowed("qq", "group", "9")
        self.assertFalse(ok)

    def test_private_not_in_whitelist_refuses(self):
        # 私聊看的是 nai_private：名单里没有就拒（nai_groups 里有也不算数）。
        with mock.patch.object(agents, "NAI_ENABLED", True), \
                mock.patch.object(agents, "load_settings", return_value=self._settings(
                    nai_enabled=True, nai_groups=["9"])):
            ok, why = agents.nai_allowed("qq", "private", "9")
        self.assertFalse(ok)
        self.assertIn("私聊", why)

    def test_private_full_allow(self):
        with mock.patch.object(agents, "NAI_ENABLED", True), \
                mock.patch.object(agents, "load_settings", return_value=self._settings(
                    nai_enabled=True, nai_private=["9"])):
            ok, _ = agents.nai_allowed("qq", "private", "9")
        self.assertTrue(ok)

    def test_web_refuses(self):
        with mock.patch.object(agents, "NAI_ENABLED", True), \
                mock.patch.object(agents, "load_settings", return_value=self._settings(
                    nai_enabled=True, nai_groups=["9"], nai_private=["9"])):
            ok, why = agents.nai_allowed("qq", None, None)
        self.assertFalse(ok)

    def test_full_allow(self):
        with mock.patch.object(agents, "NAI_ENABLED", True), \
                mock.patch.object(agents, "load_settings", return_value=self._settings(
                    nai_enabled=True, nai_groups=["9"])):
            ok, why = agents.nai_allowed("qq", "group", "9")
        self.assertTrue(ok)


class TimeoutTest(unittest.TestCase):
    """NAI 的 180 秒兜底。

    2026-09-29 用户提：「nai 怎么没有 180 秒钟的超时呀」。原来写死 (30, 240)，
    比 ComfyUI 渠道的 IMAGE_GEN_TIMEOUT(180) 还宽，于是 NAI 反而成了唯一没有
    180 秒兜底的渠道。现在 read 直接取 IMAGE_GEN_TIMEOUT——改 .env 一处就同时
    生效，两个渠道口径一致。
    """

    def test_read_timeout_follows_image_gen_timeout(self):
        from app.config import IMAGE_GEN_TIMEOUT
        self.assertEqual(nai.NAI_TIMEOUT[1], IMAGE_GEN_TIMEOUT)

    def test_timeout_is_passed_to_the_request(self):
        sess = GenerateTest._fake_session(self)
        with mock.patch.object(nai, "requests") as req, \
                mock.patch.object(nai, "NAI_PROXY", ""), \
                mock.patch.object(nai, "NAI_API_KEY", "pst-test"):
            req.Session = mock.Mock(return_value=sess)
            nai.generate("a cat")
        _, kwargs = sess.post.call_args
        self.assertEqual(kwargs["timeout"], nai.NAI_TIMEOUT)

    def test_img2img_uses_the_same_timeout(self):
        sess = GenerateTest._fake_session(self)
        with mock.patch.object(nai, "requests") as req, \
                mock.patch.object(nai, "NAI_PROXY", ""), \
                mock.patch.object(nai, "NAI_API_KEY", "pst-test"):
            req.Session = mock.Mock(return_value=sess)
            nai.generate_img2img("x", "QUJD")
        _, kwargs = sess.post.call_args
        self.assertEqual(kwargs["timeout"], nai.NAI_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
