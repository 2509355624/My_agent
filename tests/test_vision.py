# -*- coding: utf-8 -*-
"""图片识别链路测试（app/vision.py + config.provider_vision + agent._error_reply）。

关注四点：
1. 视觉能力判定——判错会让纯文本模型收到 base64，整条请求挂死（实测
   ReadTimeout 卡满 180 秒），而不是"效果差一点"；
2. 图片压缩——QQ 群里的图有 8MB 级的，不缩就变成十几 MB 的 base64 字符串；
3. 取图失败只跳过单张，不打断整轮对话；
4. 额度耗尽与限流都长着 429 的脸，但处理方式相反，不能混成一句"出错了"。
"""

import io
import unittest
from unittest import mock

import app.agent as agent
import app.config as config
import app.vision as vision
from app.agent import _error_reply

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


def _png(size=(100, 80)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


# ─── 视觉能力判定 ───────────────────────────────────

class ProviderVisionTest(unittest.TestCase):
    def test_volc_has_no_vision(self):
        self.assertFalse(
            config.provider_vision("volc", "deepseek-v4-flash-ga-260731"))
        self.assertFalse(config.provider_vision("volc", "deepseek-v4-pro"))

    def test_deepseek_official_has_vision(self):
        self.assertTrue(config.provider_vision("deepseek", "deepseek-flash"))
        # 不传 model 时取该 provider 的默认模型
        self.assertTrue(config.provider_vision("deepseek"))

    def test_model_name_hint_overrides_provider_switch(self):
        # 将来把豆包/本地换成视觉型号，不用回来改代码
        self.assertTrue(config.provider_vision("doubao", "doubao-1-5-vision-pro"))
        self.assertTrue(config.provider_vision("ollama", "qwen2.5-vl:7b"))

    def test_unknown_provider_does_not_raise(self):
        self.assertIsInstance(config.provider_vision("nonexistent", "x"), bool)

    def test_providers_expose_vision_flag(self):
        # 管理页靠这个字段在下拉里即时提示，漏了就只剩硬编码
        for pid in ("volc", "doubao", "deepseek", "ollama"):
            self.assertIn("vision", config.PROVIDERS[pid],
                          "%s 缺 vision 字段" % pid)


# ─── 压缩与类型识别 ─────────────────────────────────

class SniffMimeTest(unittest.TestCase):
    def test_known_types(self):
        self.assertEqual(vision.sniff_mime(b"\xff\xd8\xff\x00"), "image/jpeg")
        self.assertEqual(vision.sniff_mime(b"\x89PNG\r\n\x1a\n"), "image/png")
        self.assertEqual(vision.sniff_mime(b"GIF89a"), "image/gif")
        self.assertEqual(vision.sniff_mime(b"RIFF....WEBPVP8 "), "image/webp")

    def test_unknown_defaults_to_jpeg(self):
        self.assertEqual(vision.sniff_mime(b"???"), "image/jpeg")


@unittest.skipUnless(_HAS_PIL, "需要 PIL 才能验证压缩")
class ShrinkImageTest(unittest.TestCase):
    def test_large_image_is_scaled_down(self):
        raw = _png((2400, 1200))
        out, mime = vision.shrink_image(raw)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(max(Image.open(io.BytesIO(out)).size),
                         config.VISION_MAX_EDGE)
        self.assertLess(len(out), len(raw))

    def test_small_image_keeps_its_size(self):
        out, _mime = vision.shrink_image(_png((100, 80)))
        self.assertEqual(Image.open(io.BytesIO(out)).size, (100, 80))

    def test_rgba_is_flattened_onto_white(self):
        # 带透明通道的图直接存 JPEG 会变黑底（截图/贴纸尤其明显）
        buf = io.BytesIO()
        Image.new("RGBA", (60, 60), (0, 0, 0, 0)).save(buf, format="PNG")
        out, _mime = vision.shrink_image(buf.getvalue())
        im = Image.open(io.BytesIO(out))
        self.assertEqual(im.mode, "RGB")
        # JPEG 有损，不比精确值；只要接近白即可（若是黑底会接近 0）
        px = im.getpixel((10, 10))
        self.assertTrue(all(v > 240 for v in px),
                        "透明的部分应铺成白底，实际是 %r" % (px,))

    def test_undecodable_bytes_are_returned_unchanged(self):
        # 宁可让请求自己失败，也不要在这里抛异常把整轮对话打断
        raw = b"definitely not an image"
        out, mime = vision.shrink_image(raw)
        self.assertEqual(out, raw)
        self.assertEqual(mime, "image/jpeg")


@unittest.skipUnless(_HAS_PIL, "需要 PIL 才能验证 data URL")
class DataUrlTest(unittest.TestCase):
    def test_prefix_contains_mime_and_base64(self):
        url = vision.to_data_url(_png((20, 20)))
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))


# ─── 取图 ───────────────────────────────────────────

def _resp(content=b"", status=200, text=""):
    r = mock.Mock()
    r.status_code = status
    r.content = content
    r.text = text
    return r


class FetchImageTest(unittest.TestCase):
    def test_returns_raw_bytes(self):
        with mock.patch("app.vision._session.get",
                        return_value=_resp(b"\xff\xd8\xffabc")):
            self.assertEqual(vision.fetch_image("http://x/a.jpg"),
                             b"\xff\xd8\xffabc")

    def test_http_error_raises_runtime_error(self):
        with mock.patch("app.vision._session.get", return_value=_resp(status=404)):
            with self.assertRaises(RuntimeError):
                vision.fetch_image("http://x/a.jpg")

    def test_oversize_raises(self):
        with mock.patch("app.vision._session.get",
                        return_value=_resp(b"x" * 500)):
            with self.assertRaises(RuntimeError) as ctx:
                vision.fetch_image("http://x/a.jpg", max_bytes=100)
            self.assertIn("过大", str(ctx.exception))

    def test_empty_body_raises(self):
        with mock.patch("app.vision._session.get", return_value=_resp(b"")):
            with self.assertRaises(RuntimeError):
                vision.fetch_image("http://x/a.jpg")

    def test_network_error_becomes_runtime_error(self):
        with mock.patch("app.vision._session.get",
                        side_effect=OSError("connection refused")):
            with self.assertRaises(RuntimeError):
                vision.fetch_image("http://x/a.jpg")


# ─── 识图调用 ───────────────────────────────────────

class DescribeTest(unittest.TestCase):
    def _ok(self, content=" 一只猫 "):
        return _resp(status=200), {"choices": [{"message": {"content": content}}]}

    def test_returns_stripped_text(self):
        r, body = self._ok()
        r.json.return_value = body
        with mock.patch("app.vision._session.post", return_value=r):
            self.assertEqual(vision.describe("data:image/jpeg;base64,AAA"), "一只猫")

    def test_payload_carries_image_and_default_model(self):
        r, body = self._ok("ok")
        r.json.return_value = body
        with mock.patch("app.vision._session.post", return_value=r) as post:
            vision.describe("data:image/jpeg;base64,AAA")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            payload["messages"][0]["content"][1]["image_url"]["url"],
            "data:image/jpeg;base64,AAA")
        # VISION_MODEL 为空时用该 provider 的默认模型
        self.assertEqual(payload["model"],
                         config.PROVIDERS[config.VISION_PROVIDER]["model"])

    def test_http_error_raises_runtime_error(self):
        with mock.patch("app.vision._session.post",
                        return_value=_resp(status=429, text="QuotaExceeded")):
            with self.assertRaises(RuntimeError):
                vision.describe("data:image/jpeg;base64,AAA")

    def test_unparsable_body_raises_runtime_error(self):
        r = _resp(status=200)
        r.json.side_effect = ValueError("not json")
        with mock.patch("app.vision._session.post", return_value=r):
            with self.assertRaises(RuntimeError):
                vision.describe("data:image/jpeg;base64,AAA")

    def test_unknown_vision_provider_raises(self):
        with mock.patch.object(config, "VISION_PROVIDER", "nonexistent"):
            with self.assertRaises(RuntimeError):
                vision.describe("data:image/jpeg;base64,AAA")


# ─── 错误提示文案 ───────────────────────────────────

class ErrorReplyTest(unittest.TestCase):
    def test_quota_exhausted_tells_user_to_switch_model(self):
        msg = _error_reply(RuntimeError(
            "LLM 请求失败 HTTP 429 Too Many Requests | "
            "QuotaExceeded: 当前账号对该模型的免费试用额度已消耗完毕"))
        self.assertIn("额度", msg)
        self.assertIn("管理页", msg)

    def test_rate_limit_says_just_wait(self):
        msg = _error_reply(RuntimeError(
            "LLM 请求失败 HTTP 429 | RateLimitExceeded: rate limit reached"))
        self.assertIn("限流", msg)
        self.assertIn("稍等", msg)
        # 限流不该把人引去切模型——那会把免费的额度白白换掉
        self.assertNotIn("管理页", msg)

    def test_other_errors_keep_the_original_text(self):
        msg = _error_reply(RuntimeError("connection reset by peer"))
        self.assertIn("connection reset by peer", msg)


class VisionAttributionTest(unittest.TestCase):
    """识图文字块要写清「谁发的图」。

    群里一轮经常混着好几个人的图，块头写「用户发来图片」时模型只能猜主人，
    猜错就是把 A 的图安到 B 头上——这是「关系网乱」的头号来源。
    """

    def _patch_describe(self, text="一只猫"):
        p = mock.patch("app.vision.describe", return_value=text)
        p.start()
        self.addCleanup(p.stop)

    def test_head_names_the_sender(self):
        self._patch_describe()
        out = agent._with_vision("", ["data:1"], ["温知澄"])
        self.assertIn("[温知澄 发来图片，以下是识别结果]", out)
        self.assertNotIn("用户发来", out)

    def test_head_lists_two_senders(self):
        self._patch_describe()
        out = agent._with_vision("", ["data:1", "data:2"], ["温知澄", "翎"])
        self.assertIn("温知澄、翎", out)

    def test_unknown_sender_falls_back_to_generic_head(self):
        self._patch_describe()
        out = agent._with_vision("", ["data:1"])
        self.assertIn("[用户发来图片，以下是识别结果]", out)

    def test_multi_images_mark_each_owner(self):
        self._patch_describe()
        out = agent._with_vision("", ["data:1", "data:2"], ["温知澄", "翎"])
        self.assertIn("【第 1 张（温知澄 发的）】", out)
        self.assertIn("【第 2 张（翎 发的）】", out)

    def test_missing_owner_for_one_image_is_left_blank(self):
        # owners 比图少时缺的那张留空，不能错位安人
        self._patch_describe()
        out = agent._with_vision("", ["data:1", "data:2"], ["温知澄"])
        self.assertIn("【第 1 张（温知澄 发的）】", out)
        self.assertIn("【第 2 张】", out)

    def test_single_image_keeps_plain_body(self):
        self._patch_describe()
        out = agent._with_vision("帮我看看", ["data:1"], ["温知澄"])
        self.assertIn("帮我看看", out)
        self.assertIn("一只猫", out)
        self.assertNotIn("【第 1 张", out)


if __name__ == "__main__":
    unittest.main()
