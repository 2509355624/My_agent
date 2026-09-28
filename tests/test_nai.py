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

    def _run(self, sess, prompt="a cat", proxies="", key="pst-test"):
        with mock.patch.object(nai, "requests") as req, \
                mock.patch.object(nai, "NAI_PROXY", proxies), \
                mock.patch.object(nai, "NAI_API_KEY", key):
            req.Session = mock.Mock(return_value=sess)
            return nai.generate(prompt)

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
        self.assertEqual(body["model"], "nai-diffusion-5-curated")
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

    def _run(self, prompt="make it night", image="QUJD", strength=None):
        sess = GenerateTest._fake_session(self, body=self._fake_zip())
        with mock.patch.object(nai, "requests") as req, \
                mock.patch.object(nai, "NAI_PROXY", ""), \
                mock.patch.object(nai, "NAI_API_KEY", "pst-test"):
            req.Session = mock.Mock(return_value=sess)
            if strength is None:
                nai.generate_img2img(prompt, image)
            else:
                nai.generate_img2img(prompt, image, strength=strength)
        args, kwargs = sess.post.call_args
        return kwargs["json"]

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


class PrepareImageTest(unittest.TestCase):
    """nai.prepare_image：对齐 NAI 出图尺寸（居中裁剪，不拉伸）+ 纯 base64。"""

    def _png(self, w, h, mode="RGB"):
        buf = io.BytesIO()
        Image.new(mode, (w, h), (200, 120, 90)).save(buf, "PNG")
        return buf.getvalue()

    def _decode(self, b64):
        self.assertNotIn(b"://", base64.b64decode(b64)[:64])  # 纯 base64，无 data: 前缀
        return Image.open(io.BytesIO(base64.b64decode(b64)))

    def test_wide_image_is_center_cropped_not_stretched(self):
        im = self._decode(nai.prepare_image(self._png(2000, 500)))
        self.assertEqual(im.size, (nai.NAI_WIDTH, nai.NAI_HEIGHT))

    def test_tall_image_is_center_cropped(self):
        im = self._decode(nai.prepare_image(self._png(300, 1600)))
        self.assertEqual(im.size, (nai.NAI_WIDTH, nai.NAI_HEIGHT))

    def test_alpha_is_flattened(self):
        im = self._decode(nai.prepare_image(self._png(64, 64, "RGBA")))
        self.assertEqual(im.mode, "RGB")
        self.assertEqual(im.size, (nai.NAI_WIDTH, nai.NAI_HEIGHT))

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

    def test_private_refuses(self):
        with mock.patch.object(agents, "NAI_ENABLED", True), \
                mock.patch.object(agents, "load_settings", return_value=self._settings(
                    nai_enabled=True, nai_groups=["9"])):
            ok, _ = agents.nai_allowed("qq", "private", "9")
        self.assertFalse(ok)

    def test_full_allow(self):
        with mock.patch.object(agents, "NAI_ENABLED", True), \
                mock.patch.object(agents, "load_settings", return_value=self._settings(
                    nai_enabled=True, nai_groups=["9"])):
            ok, why = agents.nai_allowed("qq", "group", "9")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
