# -*- coding: utf-8 -*-
"""app/image_out.py：对外发图前的重编码（jpg / png）与回落。

这个模块是「图不把工作流带出去」的唯一落点，两条底线都得钉住：
转成功必须是目标格式、且不带 PNG 里的 tEXt 工作流元数据；转失败必须
回落成 ComfyUI 原图 URL（图还得发得出去，不能卡在这一步）。

png 那条路还多一条：必须是**无损**的——选 png 的全部意义就在这。

零网络：下载那一层整个 mock 掉，只用内存里的假 PNG 当素材。
"""

import io
import os
import time
import unittest
from unittest import mock

from PIL import Image, PngImagePlugin

from app import image_out


def _png_bytes(with_meta=True, mode="RGB", size=(32, 32), color=None):
    """造一张像 ComfyUI 产出的 PNG —— 关键是带着 tEXt 元数据。"""
    if color is None:
        color = (200, 30, 60) if mode == "RGB" else (200, 30, 60, 255)
    im = Image.new(mode, size, color)
    buf = io.BytesIO()
    kw = {}
    if with_meta:
        info = PngImagePlugin.PngInfo()
        info.add_text("prompt", '{"1": {"class_type": "VAELoader"}}')
        info.add_text("workflow", '{"nodes": []}')
        kw["pnginfo"] = info
    im.save(buf, "PNG", **kw)
    return buf.getvalue()


class _Resp:
    """requests 响应的最小替身。"""

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.d = image_out._out_dir()
        self.before = set(os.listdir(self.d))

    def tearDown(self):
        # 产物落在系统 temp 里，测试自己收拾干净，不给下回留垃圾
        for name in set(os.listdir(self.d)) - self.before:
            try:
                os.remove(os.path.join(self.d, name))
            except OSError:
                pass

    def _run(self, resp=None, exc=None, filename="Anima_00001_.png", fmt=None):
        get = (mock.Mock(side_effect=exc) if exc is not None
               else mock.Mock(return_value=resp))
        with mock.patch.object(image_out._session, "get", get):
            if fmt is None:
                return image_out.prepare_for_send(filename)
            return image_out.prepare_for_send(filename, fmt)

    def test_converts_to_jpeg_and_drops_metadata(self):
        got = self._run(_Resp(_png_bytes()))
        self.assertTrue(os.path.isfile(got), got)
        self.assertTrue(got.lower().endswith(".jpg"))
        with Image.open(got) as im:
            self.assertEqual(im.format, "JPEG")
            self.assertNotIn("prompt", im.info)
            self.assertNotIn("workflow", im.info)

    def test_keeps_pixel_size(self):
        got = self._run(_Resp(_png_bytes(size=(64, 48))))
        with Image.open(got) as im:
            self.assertEqual(im.size, (64, 48))

    def test_falls_back_to_view_url_when_request_fails(self):
        """ComfyUI 拉不到图（或整个不可达）时不能把图卡住。"""
        got = self._run(exc=OSError("connection refused"))
        self.assertTrue(got.startswith("http"))
        self.assertIn("/view?filename=Anima_00001_.png", got)

    def test_falls_back_to_view_url_when_vae_outputs_file(self):
        """ComfyUI 返回的不是图片（比如 404 的 JSON）时同样回落。"""
        got = self._run(_Resp(b'{"error": "not found"}'))
        self.assertIn("/view?filename=Anima_00001_.png", got)

    def test_filename_is_url_encoded_in_fallback(self):
        got = self._run(exc=OSError("boom"), filename="a b&c.png")
        self.assertIn("a%20b%26c.png", got)

    def test_alpha_png_gets_white_background(self):
        """带透明的图直接转 RGB 会把透明区压成黑块，看着像坏图。"""
        im = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        got = self._run(_Resp(buf.getvalue()))
        rgb = Image.open(got).convert("RGB")
        self.assertTrue(all(c > 240 for c in rgb.getpixel((8, 8))),
                        rgb.getpixel((8, 8)))

    def test_png_format_drops_metadata(self):
        """切成 png 也不能把工作流带出去——这才是这个模块存在的理由。"""
        got = self._run(_Resp(_png_bytes()), fmt="png")
        self.assertTrue(os.path.isfile(got), got)
        self.assertTrue(got.lower().endswith(".png"))
        with Image.open(got) as im:
            self.assertEqual(im.format, "PNG")
            self.assertNotIn("prompt", im.info)
            self.assertNotIn("workflow", im.info)

    def test_png_format_is_lossless(self):
        """无损 = 像素逐点一致；不然凭什么比 jpg 大十倍。"""
        src = _png_bytes(size=(48, 40))
        got = self._run(_Resp(src), fmt="png")
        with Image.open(got) as im:
            out = im.convert("RGB").tobytes()
        with Image.open(io.BytesIO(src)) as im:
            want = im.convert("RGB").tobytes()
        self.assertEqual(out, want)

    def test_png_format_keeps_alpha(self):
        """jpg 那条路要铺白底；png 是无损，透明通道得留着。"""
        im = Image.new("RGBA", (16, 16), (10, 20, 30, 0))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        got = self._run(_Resp(buf.getvalue()), fmt="png")
        with Image.open(got) as out:
            self.assertEqual(out.mode, "RGBA")
            self.assertEqual(out.getpixel((8, 8)), (10, 20, 30, 0))

    def test_png_format_keeps_pixel_size(self):
        got = self._run(_Resp(_png_bytes(size=(70, 30))), fmt="png")
        with Image.open(got) as im:
            self.assertEqual(im.size, (70, 30))

    def test_unknown_format_falls_back_to_jpg(self):
        """野值不能透给 PIL 的 save(format=...)——那会直接抛错把图卡住。"""
        got = self._run(_Resp(_png_bytes()), fmt="webp")
        self.assertTrue(got.lower().endswith(".jpg"), got)

    def test_png_format_falls_back_to_view_url_on_failure(self):
        got = self._run(exc=OSError("connection refused"), fmt="png")
        self.assertIn("/view?filename=Anima_00001_.png", got)

    def test_sweep_drops_expired_and_keeps_fresh(self):
        old = os.path.join(self.d, "old.jpg")
        fresh = os.path.join(self.d, "fresh.jpg")
        open(old, "wb").close()
        open(fresh, "wb").close()
        past = time.time() - image_out.KEEP_SECONDS - 10
        os.utime(old, (past, past))
        image_out._sweep(self.d)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_session_ignores_env_proxy(self):
        """回环地址不能被系统代理劫持（本机常驻 Clash 之类，一劫持就 502）。"""
        self.assertFalse(image_out._session.trust_env)
