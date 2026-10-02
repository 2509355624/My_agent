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
import os
import tempfile
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

    def test_mimo_model_is_vision_even_without_mimo_provider(self):
        # 只在网页面板覆盖了模型（mimo-v2.6-flash）但 provider 仍是默认值时，
        # 不能误判成非视觉而多跑一道识图预处理。mimo 全系列全模态。
        self.assertTrue(config.provider_vision("volc", "mimo-v2.6-flash"))
        self.assertTrue(config.provider_vision("deepseek", "mimo-v2.6-flash"))

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


class LocalFileUrlTest(unittest.TestCase):
    """引用**机器人自己发的图**时，协议端回传的是 file://，得能读盘。

    背景：2026-09-30~10-01 的日志里有 44 次「No connection adapters were
    found for 'file:///...'」——fetch_image 只认 http，而机器人自己发的图走的是
    image_out 的临时目录（见 vision._local_image_path）。这条最常用的路
    （引用刚发的那张来改）当时一次都没通过。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch("app.image_out._out_dir",
                             return_value=self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _uri(self, name, raw=b"\x89PNG fake"):
        path = os.path.join(self.dir.name, name)
        with open(path, "wb") as f:
            f.write(raw)
        return "file:///" + path.replace("\\", "/")

    def test_reads_the_local_copy(self):
        uri = self._uri("Anima_00471__bcaae650.png")
        with mock.patch("app.vision._session.get") as get:
            self.assertEqual(vision.fetch_image(uri), b"\x89PNG fake")
            get.assert_not_called()

    def test_percent_escapes_are_decoded(self):
        uri = self._uri("a b.png", b"spaced")
        with mock.patch("app.vision._session.get"):
            self.assertEqual(vision.fetch_image(uri), b"spaced")

    def test_expired_copy_says_so(self):
        uri = "file:///" + os.path.join(
            self.dir.name, "gone.png").replace("\\", "/")
        with mock.patch("app.vision._session.get"):
            with self.assertRaises(RuntimeError) as ctx:
                vision.fetch_image(uri)
            self.assertIn("过期", str(ctx.exception))

    def test_oversize_local_copy_raises(self):
        uri = self._uri("big.png", b"x" * 500)
        with mock.patch("app.vision._session.get"):
            with self.assertRaises(RuntimeError) as ctx:
                vision.fetch_image(uri, max_bytes=100)
            self.assertIn("过大", str(ctx.exception))

    def test_outside_our_dir_is_not_read(self):
        # file:// 能读任意本机文件，而 QQ 段的 url 是外部输入——只认自家目录。
        fh = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        fh.write(b"secret")
        fh.close()
        self.addCleanup(os.remove, fh.name)
        uri = "file:///" + fh.name.replace("\\", "/")
        with mock.patch("app.vision._session.get",
                        return_value=_resp(status=404)) as get:
            with self.assertRaises(RuntimeError):
                vision.fetch_image(uri)
            get.assert_called_once()


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
        # VISION_MODEL 为空时用该 provider 的默认模型。
        # provider id 大小写不敏感（app/vision.py 里 .lower() 后再查 PROVIDERS），
        # .env 里写成 "MiMo" 也能命中 "mimo"——这里跟代码同口径。
        self.assertEqual(
            payload["model"],
            config.PROVIDERS[config.VISION_PROVIDER.lower()]["model"])

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


# ─── 总超时闸门 ─────────────────────────────────────

class VisionDeadlineTest(unittest.TestCase):
    """识图/取图必须走「整次请求的总上限」，而不是「单个 socket 操作上限」。

    这是 2026-09-29 那次「群聊一轮 298 秒」的根因：传数字超时的时候，建连能
    烧满一次、读又能再烧满一次，一次识图吃掉 240 秒，把整条会话线堵死。
    """

    def _ok(self):
        r = _resp(status=200)
        r.json.return_value = {"choices": [{"message": {"content": "猫"}}]}
        return r

    def test_describe_passes_a_total_deadline(self):
        with mock.patch("app.vision._session.post",
                        return_value=self._ok()) as post:
            vision.describe("data:image/jpeg;base64,AAA")
        t = post.call_args.kwargs["timeout"]
        self.assertEqual(t.total, vision.VISION_TIMEOUT)
        # 建连不能吃掉整个总预算，否则「连不上」自己就能耗满总时长
        self.assertLessEqual(t.connect_timeout, t.total)

    def test_describe_honours_explicit_timeout(self):
        with mock.patch("app.vision._session.post",
                        return_value=self._ok()) as post:
            vision.describe("data:image/jpeg;base64,AAA", timeout=7)
        self.assertEqual(post.call_args.kwargs["timeout"].total, 7)

    def test_fetch_image_passes_a_total_deadline(self):
        with mock.patch("app.vision._session.get",
                        return_value=_resp(b"\xff\xd8\xffabc")) as get:
            vision.fetch_image("http://x/a.jpg")
        self.assertEqual(get.call_args.kwargs["timeout"].total,
                         config.QQ_IMAGE_TIMEOUT)

    def test_connect_never_exceeds_total(self):
        """total 比 connect 还小时，不能构造出 connect > total 的自相矛盾配置。"""
        t = vision._deadline(3)
        self.assertEqual(t.total, 3)
        self.assertLessEqual(t.connect_timeout, 3)

    def test_timeout_failure_carries_elapsed(self):
        """卡住之后要能看出「卡了多久」——异常里带耗时，会一路传到 agent 的告警。"""
        with mock.patch("app.vision._session.post",
                        side_effect=OSError("Read timed out")):
            with self.assertRaises(RuntimeError) as ctx:
                vision.describe("data:image/jpeg;base64,AAA")
        self.assertIn("耗时", str(ctx.exception))


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


class VisionRefusalFilterTest(unittest.TestCase):
    """识图模型「看到了但按内容政策拒绝描述」时，拒绝话术不能流进下游。

    私聊发露骨图几乎每张都触发。实测 8 次带图返回：6 次纯拒绝、1 次能力失败
    （「我还没有学会回答这个问题」）、1 次是**拒绝开头 + 有效内容**（拒绝描述
    身体，但照用户问的那句答了头发，下游据此把提示词补了 46 字）。

    所以口径是：整段丢掉会连那次有用的内容一起扔；一句不丢又会让「我无法描述
    这张色情图片」被当成画面描述去生图。做法是按句剥离（agent._vision_usable）。
    """

    # 实测原话，逐条钉住
    _PURE_REFUSALS = [
        "我无法按要求描述这张图片。图片包含露骨的色情内容，不符合我的安全准则，"
        "因此我不能提供详细描述或提取其中的文字。如果你有其他合规的图片需要描述，"
        "我可以帮忙。",
        "你所提供的内容包含色情低俗、违背公序良俗的不良信息，这类内容不符合相关"
        "规范和健康的交流导向，我不能按照你的要求进行描述和提取相关内容，请你提供"
        "合规、健康的内容进行交流。",
        "这张图包含色情裸露与性暗示内容，属于不适宜公开描述的范围，我无法为你逐项"
        "展开画面细节或提取其中的文字。如果你有合规的图片需要描述，可以换一张，"
        "我会继续帮你。",
        "抱歉，我无法按这个要求描述或补充该图片。图片内容涉及色情低俗画面，不符合"
        "内容安全规范。如果你需要，我可以帮你处理其他合规的图片描述或文字提取需求。",
    ]

    def test_pure_refusals_are_dropped(self):
        for text in self._PURE_REFUSALS:
            self.assertEqual(agent._vision_usable(text), "", text[:30])

    def test_capability_failure_is_dropped(self):
        """「我还没有学会回答这个问题」+ 客套收尾 —— 也当没读到。"""
        text = ("对不起，我还没有学会回答这个问题。"
                "如果你有其他问题，我非常乐意为你提供帮助。")
        self.assertEqual(agent._vision_usable(text), "")

    def test_refusal_plus_real_content_keeps_the_content(self):
        """混合返回里那半段有效内容必须留——它是唯一有用的那次。"""
        refusal = "这张图片包含露骨的色情内容，我无法按你的要求进行完整描述或提取细节。"
        bad = "图中画面涉及裸露、性暗示姿势及液体等不适宜元素，不符合内容安全规范。"
        good = ("关于你提到的头发问题——从可见画面来看，角色后脑勺到马尾区域的发丝"
                "确实存在明显的结构混乱：发束走向不自然、发丝与发饰边缘融合模糊、"
                "部分发丝出现断裂或重复叠加的伪影。")
        out = agent._vision_usable(refusal + bad + good)
        self.assertIn("发束走向不自然", out)
        self.assertNotIn("我无法按你的要求", out)
        self.assertNotIn("色情", out)

    def test_blind_result_is_dropped(self):
        blind = ("我无法直接看到图片，但根据你提供的引用信息和需求，我可以帮你梳理"
                 "关键点：原图是粉色小熊连体泳衣。")
        self.assertEqual(agent._vision_usable(blind), "")

    def test_short_valid_description_survives(self):
        """有效返回可以短到「一只猫」，不能因为凑不够下限就当成没读到。"""
        self.assertEqual(agent._vision_usable("一只猫"), "一只猫")
        self.assertEqual(agent._vision_usable("一只猫趴在键盘上。"),
                         "一只猫趴在键盘上。")

    def test_valid_description_is_not_reformatted(self):
        """没命中任何拒绝话术时正文一个字节都不许动（空行/换行原样留）。"""
        text = "画面内容描述：\n一只猫。\n\n原样提取文字：\n喵"
        self.assertEqual(agent._vision_usable(text), text)

    def test_if_you_need_to_explain_is_not_a_refusal(self):
        """「如果你需要让文本模型理解这张图，可以概括为…」是有效内容，不是拒绝收尾。"""
        text = ("这是一张经典暴走漫画风格的简笔画表情包，画面主体是一个用粗黑线条"
                "勾勒的简笔画人头，眼神呈现出一种斜视、偷瞄的表情。"
                "如果你需要让文本模型理解这张图，可以将其概括为「一个歪眼斜视、"
                "流着口水、脸上泛红傻笑的简笔画暴漫表情」。")
        out = agent._vision_usable(text)
        self.assertIn("如果你需要让文本模型理解这张图", out)

    def test_refusal_becomes_failure_note_end_to_end(self):
        """走完 _with_vision：拒绝话术进不了拼给下游的文本。"""
        p = mock.patch("app.vision.describe",
                       return_value=self._PURE_REFUSALS[0])
        p.start()
        self.addCleanup(p.stop)
        out = agent._with_vision("帮我看看", ["data:1"], ["温知澄"])
        self.assertIn("识别失败", out)
        self.assertNotIn("色情", out)

    def test_mixed_result_keeps_content_end_to_end(self):
        """混合返回：有效那半段照常进下游，拒绝那半段不留。"""
        mixed = ("这张图片包含露骨的色情内容，我无法按你的要求进行完整描述。"
                 "角色后脑勺到马尾区域的发丝存在明显的结构混乱，发束走向不自然。")
        p = mock.patch("app.vision.describe", return_value=mixed)
        p.start()
        self.addCleanup(p.stop)
        out = agent._with_vision("帮我看看", ["data:1"], ["温知澄"])
        self.assertIn("发束走向不自然", out)
        self.assertNotIn("色情", out)


class VisionQuestionTest(unittest.TestCase):
    """识图要把用户的问题带进去。

    不带问题的话，识图只按通用指令读图，下游文本模型拿到的是一段泛泛的
    描述，得自己猜用户想问什么。但「原样提取文字」这条硬要求不能因为加了
    问题就丢——报错截图里的英文被翻译过，agent 就再也搜不到那个错误码。
    """

    def test_no_question_falls_back_to_generic_prompt(self):
        # 只发图不打字是常见用法，不能拼出一个空的「用户的需求：」
        self.assertEqual(vision.build_prompt(""), vision._PROMPT)
        self.assertEqual(vision.build_prompt("   "), vision._PROMPT)
        self.assertNotIn("用户的需求", vision.build_prompt(""))

    def test_question_is_embedded(self):
        p = vision.build_prompt("这个报错啥意思")
        self.assertIn("这个报错啥意思", p)
        self.assertIn("用户的需求", p)

    def test_hard_requirement_survives_the_question(self):
        # 加问题不能把「原样提取文字」挤掉
        p = vision.build_prompt("这图里写了啥")
        self.assertIn("原样提取", p)
        self.assertIn("不要翻译", p)

    def test_long_question_is_truncated(self):
        long_q = "问" * (vision.QUESTION_MAX_CHARS + 50)
        p = vision.build_prompt(long_q)
        self.assertNotIn("问" * (vision.QUESTION_MAX_CHARS + 1), p)
        self.assertIn("…", p)

    def test_multi_image_marks_position(self):
        p = vision.build_prompt("看看这个", 2, 3)
        self.assertIn("第 2 张", p)
        self.assertIn("共 3 张", p)
        # 单张不加这句噪音
        self.assertNotIn("第 1 张", vision.build_prompt("看看这个", 1, 1))

    def test_with_vision_passes_question_into_prompt(self):
        seen = {}

        def fake(data_url, prompt=None, timeout=None):
            seen["prompt"] = prompt
            return "一只猫"

        with mock.patch("app.vision.describe", side_effect=fake):
            agent._with_vision("这个报错啥意思", ["data:1"])
        self.assertIn("这个报错啥意思", seen["prompt"])

    def test_each_image_carries_its_index(self):
        prompts = []

        def fake(data_url, prompt=None, timeout=None):
            prompts.append(prompt)
            return "一只猫"

        with mock.patch("app.vision.describe", side_effect=fake):
            agent._with_vision("看看", ["data:1", "data:2"])
        self.assertEqual(len(prompts), 2)
        self.assertIn("第 1 张", prompts[0])
        self.assertIn("第 2 张", prompts[1])

    def test_no_question_keeps_generic_prompt_in_call(self):
        seen = {}

        def fake(data_url, prompt=None, timeout=None):
            seen["prompt"] = prompt
            return "一只猫"

        with mock.patch("app.vision.describe", side_effect=fake):
            agent._with_vision("", ["data:1"])
        self.assertEqual(seen["prompt"], vision._PROMPT)


if __name__ == "__main__":
    unittest.main()
