"""图生图：源图解析 / 缩放 / 上传，以及工具里的分流与占位符注入。

零网络、零落盘：图片下载与 ComfyUI 上传全部 mock 掉。这里唯一碰磁盘的是
「本地路径」那两条用例——它们故意拿本测试文件自己当那张图。
"""

import io
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

from app import comfy_src, qq_api, stickers, vision
from app.skills import load_workflow
from app.tools.normal import generate_image as gi


def _png(w, h, mode="RGB"):
    fill = (200, 120, 90) if mode == "RGB" else (200, 120, 90, 128)
    buf = io.BytesIO()
    Image.new(mode, (w, h), fill).save(buf, "PNG")
    return buf.getvalue()


class PickTest(unittest.TestCase):
    def test_blank_and_synonyms_mean_the_first(self):
        for spec in ("", "最新", "最近", "这张", "last", "第一张"):
            self.assertEqual(comfy_src._pick(spec, 3), 0)

    def test_number_counts_forward(self):
        self.assertEqual(comfy_src._pick("1", 3), 0)
        self.assertEqual(comfy_src._pick("2", 3), 1)
        self.assertEqual(comfy_src._pick("3", 3), 2)

    def test_out_of_range_names_the_count(self):
        with self.assertRaises(RuntimeError) as cm:
            comfy_src._pick("4", 3)
        self.assertIn("没有第 4 张", str(cm.exception))

    def test_not_a_number(self):
        with self.assertRaises(RuntimeError) as cm:
            comfy_src._pick("随便", 3)
        self.assertIn("只能填数字", str(cm.exception))


class ResolveTest(unittest.TestCase):
    KEY = ("group", 999001)

    def setUp(self):
        self._saved = dict(stickers._RECENT_IMAGES)

    def tearDown(self):
        stickers._RECENT_IMAGES.clear()
        stickers._RECENT_IMAGES.update(self._saved)

    def _qq(self, quoted=None):
        """假装此刻正在处理一个 QQ 会话，并给出**本轮引用**到的那几张图。"""
        return (
            mock.patch.object(qq_api, "current_context", lambda: self.KEY),
            mock.patch.object(qq_api, "current_quoted_images",
                              lambda: list(quoted or [])),
        )

    def test_qq_lone_quote_is_just_the_quoted_one(self):
        p1, p2 = self._qq(["http://img/a.jpg"])
        with p1, p2, mock.patch.object(vision, "fetch_image",
                                       lambda u: b"RAW:" + u.encode()):
            raw, note = comfy_src.resolve("1")
        self.assertEqual(raw, b"RAW:http://img/a.jpg")
        self.assertEqual(note, "引用的那张图")

    def test_qq_number_walks_forward(self):
        p1, p2 = self._qq(["http://img/a.jpg", "http://img/b.jpg"])
        with p1, p2, mock.patch.object(vision, "fetch_image",
                                       lambda u: b"RAW:" + u.encode()):
            raw, note = comfy_src.resolve("2")
        self.assertEqual(raw, b"RAW:http://img/b.jpg")
        self.assertIn("第 2 张", note)

    def test_qq_without_quote_asks_for_a_reference(self):
        p1, p2 = self._qq([])
        with p1, p2:
            with self.assertRaises(RuntimeError) as cm:
                comfy_src.resolve("1")
        self.assertIn("引用", str(cm.exception))

    def test_qq_ignores_images_merely_sent_earlier(self):
        """核心契约：自己发的图不算数，绝不退回「本会话最近那张」。"""
        stickers.note_image(self.KEY, "http://img/loose.jpg", "233")
        p1, p2 = self._qq([])
        with p1, p2:
            with self.assertRaises(RuntimeError):
                comfy_src.resolve("1")

    def test_qq_out_of_range_is_named(self):
        p1, p2 = self._qq(["http://img/a.jpg"])
        with p1, p2:
            with self.assertRaises(RuntimeError) as cm:
                comfy_src.resolve("2")
        self.assertIn("有 1 张图", str(cm.exception))

    def test_qq_refuses_links_and_paths(self):
        p1, p2 = self._qq(["http://img/a.jpg"])
        with p1, p2:
            for spec in ("http://evil/x.png", "C:\\Windows\\win.ini",
                         "/etc/passwd"):
                with self.assertRaises(RuntimeError) as cm:
                    comfy_src.resolve(spec)
                self.assertIn("不能填链接或路径", str(cm.exception))

    def test_web_takes_local_path(self):
        with mock.patch.object(qq_api, "current_context", lambda: (None, None)):
            raw, note = comfy_src.resolve(__file__)
        self.assertGreater(len(raw), 0)
        self.assertIn("本地文件", note)

    def test_web_takes_url(self):
        with mock.patch.object(qq_api, "current_context", lambda: (None, None)), \
                mock.patch.object(vision, "fetch_image", lambda u: b"RAW"):
            raw, note = comfy_src.resolve("https://x/y.png")
        self.assertEqual(raw, b"RAW")
        self.assertIn("链接", note)

    def test_web_blank_and_missing_file_are_errors(self):
        with mock.patch.object(qq_api, "current_context", lambda: (None, None)):
            with self.assertRaises(RuntimeError):
                comfy_src.resolve("")
            with self.assertRaises(RuntimeError) as cm:
                comfy_src.resolve(os.path.join(tempfile.gettempdir(),
                                               "wb_no_such_file.png"))
        self.assertIn("找不到这个文件", str(cm.exception))


class FitTest(unittest.TestCase):
    def test_long_side_fits_and_aspect_kept(self):
        _, (w, h) = comfy_src.fit(_png(800, 400))
        self.assertEqual((w, h), (1216, 608))
        self.assertEqual(w % 8, 0)
        self.assertEqual(h % 8, 0)

    def test_portrait_long_side_is_height(self):
        _, (w, h) = comfy_src.fit(_png(768, 1536))
        self.assertEqual(h, 1216)
        self.assertLess(w, h)

    def test_small_image_is_enlarged(self):
        _, (w, h) = comfy_src.fit(_png(100, 100))
        self.assertEqual((w, h), (1216, 1216))

    def test_alpha_is_flattened(self):
        raw, _ = comfy_src.fit(_png(64, 64, "RGBA"))
        im = Image.open(io.BytesIO(raw))
        self.assertEqual(im.mode, "RGB")

    def test_garbage_raises(self):
        with self.assertRaises(RuntimeError) as cm:
            comfy_src.fit(b"definitely not an image")
        self.assertIn("读不出来", str(cm.exception))


class UploadTest(unittest.TestCase):
    class _Resp(object):
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def _post(self, payload, seen):
        def fake(url, files=None, data=None, timeout=None):
            seen["url"] = url
            seen["files"] = files
            seen["data"] = data
            return self._Resp(payload)
        return fake

    def test_posts_bytes_and_returns_name(self):
        seen = {}
        payload = {"name": "i2isrc_ab.png", "subfolder": "", "type": "input"}
        with mock.patch.object(comfy_src._session, "post",
                               self._post(payload, seen)):
            name = comfy_src.upload(b"PNGDATA")
        self.assertEqual(name, "i2isrc_ab.png")
        self.assertTrue(seen["url"].endswith("/upload/image"))
        self.assertEqual(seen["files"]["image"][1], b"PNGDATA")
        self.assertTrue(seen["files"]["image"][0].startswith("i2isrc_"))
        self.assertEqual(seen["data"], {"overwrite": "true"})

    def test_subfolder_is_prefixed(self):
        seen = {}
        payload = {"name": "i2isrc_ab.png", "subfolder": "sub", "type": "input"}
        with mock.patch.object(comfy_src._session, "post",
                               self._post(payload, seen)):
            name = comfy_src.upload(b"x")
        self.assertEqual(name, "sub/i2isrc_ab.png")

    def test_failure_raises_runtime_error(self):
        def boom(*a, **kw):
            raise OSError("connection refused")
        with mock.patch.object(comfy_src._session, "post", boom):
            with self.assertRaises(RuntimeError) as cm:
                comfy_src.upload(b"x")
        self.assertIn("传不进 ComfyUI", str(cm.exception))


class DenoiseTest(unittest.TestCase):
    def test_default(self):
        self.assertEqual(gi._denoise_value(None), "0.60")
        self.assertEqual(gi._denoise_value(""), "0.60")
        self.assertEqual(gi._denoise_value("  "), "0.60")

    def test_values(self):
        self.assertEqual(gi._denoise_value(0.45), "0.45")
        self.assertEqual(gi._denoise_value("0.8"), "0.80")

    def test_out_of_range_and_junk(self):
        for bad in (0, -1, 1.5, "abc", "high"):
            with self.assertRaises(ValueError):
                gi._denoise_value(bad)


class LoadWorkflowTest(unittest.TestCase):
    def test_missing_returns_none(self):
        self.assertIsNone(load_workflow(os.path.join("skills", "nope",
                                                     "workflow.json")))

    def test_broken_json_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{ 这不是 json")
            self.assertIsNone(load_workflow(p))

    def test_bare_seed_placeholder_survives_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "wf.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"1": {"class_type": "KSampler", '
                        '"inputs": {"seed": __SEED__}}}')
            wf = load_workflow(p)
        self.assertEqual(wf["1"]["inputs"]["seed"], "__SEED__")


class I2IFlowTest(unittest.TestCase):
    """跑 generate_image 本体，拦住提交那一刻看它到底送了什么。"""

    KEY = ("group", 999001)

    def _run(self, **kw):
        captured = {}

        def fake_queue(workflow):
            captured.update(workflow)
            return "pid-i2i"

        for patcher in (
            mock.patch.object(gi, "_qq_gate", lambda: None),
            mock.patch.object(gi, "_queue_prompt", fake_queue),
            mock.patch.object(gi, "is_cancelled", lambda: False),
            mock.patch.object(qq_api, "current_context", lambda: self.KEY),
            mock.patch.object(gi.image_jobs, "submit",
                              lambda t, i, p, force=False: (True, 0)),
            # 名额预检查读的是模块级 _inflight，钉成 0 免受别的用例影响
            mock.patch.object(gi.image_jobs, "inflight_count", lambda t, i: 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return gi._generate_image(**kw), captured

    def _source_ok(self, name="i2isrc_x.png"):
        return (
            mock.patch.object(comfy_src, "resolve",
                              lambda spec: (b"RAW", "233 发的图")),
            mock.patch.object(comfy_src, "fit",
                              lambda raw, **kw: (b"FIT", (1216, 1216))),
            mock.patch.object(comfy_src, "upload", lambda raw, **kw: name),
        )

    def test_default_call_is_still_text2img(self):
        out, wf = self._run(prompt="1girl, solo")
        self.assertIn("已经在画了", out)
        self.assertEqual(wf["9"]["class_type"], "EmptyLatentImage")
        self.assertNotIn("24", wf)
        self.assertEqual(wf["4"]["inputs"]["text"], "@kibro, 1girl, solo")

    def test_anima_switches_to_i2i_workflow(self):
        p1, p2, p3 = self._source_ok()
        with p1, p2, p3:
            out, wf = self._run(prompt="cherry blossoms", source_image="1")
        self.assertIn("已经在画了", out)
        self.assertIn("233 发的图", out)
        self.assertNotIn("9", wf)                       # 空 latent 被顶掉了
        self.assertEqual(wf["25"]["class_type"], "VAEEncode")
        self.assertEqual(wf["24"]["inputs"]["image"], "i2isrc_x.png")
        self.assertEqual(wf["2"]["inputs"]["denoise"], 0.6)
        # 图生图不做双采样：第二段（LatentUpscaleBy 1.2 → 换 anima13 精修）
        # 已删。源图本来就大（长边 1216），再叠一段必然爆显存。
        self.assertNotIn("18", wf)
        self.assertNotIn("19", wf)
        self.assertNotIn("20", wf)
        self.assertEqual(wf["3"]["inputs"]["samples"][0], "2")   # 解码直吃第一段
        # 种子是「带引号的数字串」——load_skill 的 __SEED__ 预处理留下的形态，
        # 既有的文生图一直这么提交，ComfyUI 会转成 INT。这里只钉「换了新种子」。
        self.assertTrue(str(wf["2"]["inputs"]["seed"]).isdigit())
        self.assertEqual(wf["4"]["inputs"]["text"],
                         "@kibro, cherry blossoms")

    def test_denoise_parameter_is_injected(self):
        p1, p2, p3 = self._source_ok()
        with p1, p2, p3:
            _, wf = self._run(prompt="x", source_image="2", denoise=0.85)
        self.assertEqual(wf["2"]["inputs"]["denoise"], 0.85)

    def test_sd_i2i_keeps_base_and_loras(self):
        p1, p2, p3 = self._source_ok()
        with p1, p2, p3:
            _, wf = self._run(prompt="night city", skill="image_gen_v1",
                              source_image="1")
        self.assertTrue(wf["1"]["inputs"]["ckpt_name"]
                        .endswith("v80.safetensors"))
        self.assertEqual(wf["7"]["class_type"], "LoadImage")
        self.assertEqual(wf["9"]["inputs"]["denoise"], 0.6)
        self.assertEqual(wf["2"]["inputs"]["lora_name"],
                         "add_contrast_XL.safetensors")
        self.assertIn("night city", wf["5"]["inputs"]["text"])
        # 也只有一次采样：图生图这条路不叠第二段
        samplers = [n for n in wf.values()
                    if n.get("class_type") == "KSampler"]
        self.assertEqual(len(samplers), 1)

    def test_krea2_is_refused(self):
        out, wf = self._run(prompt="x", skill="krea2", source_image="1")
        self.assertIn("只有 anima 和 image_gen_v1 支持图生图", out)
        self.assertEqual(wf, {})

    def test_source_failure_reports_and_submits_nothing(self):
        with mock.patch.object(comfy_src, "resolve",
                               mock.Mock(side_effect=RuntimeError("这儿没有图"))):
            out, wf = self._run(prompt="x", source_image="1")
        self.assertEqual(out, "这儿没有图")
        self.assertEqual(wf, {})

    def test_bad_denoise_is_caught_before_upload(self):
        touched = []
        with mock.patch.object(
                comfy_src, "resolve",
                lambda spec: (touched.append(spec), (b"R", ""))[1]):
            out, wf = self._run(prompt="x", source_image="1", denoise="9")
        self.assertIn("denoise", out)
        self.assertEqual(touched, [])
        self.assertEqual(wf, {})


if __name__ == "__main__":
    unittest.main()
