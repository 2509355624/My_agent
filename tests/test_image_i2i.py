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

from app import agents, comfy_src, nai, qq_api, stickers, vision
from app.config import DISABLED_IMAGE_SKILLS
from app.skills import load_workflow
from app.tools.normal import generate_image as gi

# 图生图已**整体停用**（`generate_image._I2I_SKILLS` 为空，见它的注释），所以
# 下面那些「qwen 图生图怎么工作」的用例测的功能眼下不存在了。
#
# **跳过而不是删掉**：摘停用是可逆的——把 qwen 填回 `_I2I_SKILLS`、清掉
# `config.DISABLED_IMAGE_SKILLS`，这些用例该自动重新跑起来。
# 「传了 source_image 一律拒」这条新合同由 I2IFlowTest 覆盖（它是活的，不跳过）。
_qwen_off = unittest.skipIf(
    not gi._I2I_SKILLS,
    "图生图已整体停用（generate_image._I2I_SKILLS 为空）")


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


class _I2IRunner(object):
    """跑 generate_image 本体，拦住提交那一刻看它到底送了什么。

    故意不是 TestCase——两个 i2i 用例类（只留 qwen 的拒收 / qwen 编辑）共用这套
    拦截，直接继承 TestCase 的话基类的用例会在子类里再跑一遍。
    """

    KEY = ("group", 999001)

    def _run(self, **kw):
        captured = {}

        def fake_queue(workflow):
            captured.update(workflow)
            return "pid-i2i"

        gi.image_jobs._reset()
        for patcher in (
            mock.patch.object(gi, "_qq_gate", lambda: None),
            # 入队前探活 / 提交前查显存水位：不挡就会真去 GET 真机的
            # /system_stats——本模块自称「零网络」，这两条是漏网的。
            mock.patch.object(gi.image_jobs, "comfy_alive", lambda: True),
            mock.patch.object(gi.image_jobs, "_free_vram_gb", lambda: None),
            # 提交动作现在发生在 image_jobs 的 worker 里，所以拦的是它那边
            mock.patch.object(gi.image_jobs, "_queue_prompt", fake_queue),
            mock.patch.object(gi.image_jobs, "_ensure_worker", lambda: None),
            mock.patch.object(gi.image_jobs, "wait_done",
                              lambda pid, timeout=None: {"outputs": {}}),
            mock.patch.object(gi.image_jobs, "_send_text", lambda *a: None),
            mock.patch.object(gi, "is_cancelled", lambda: False),
            mock.patch.object(qq_api, "current_context", lambda: self.KEY),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        out = gi._generate_image(**kw)
        gi.image_jobs._drain()          # 队列是同步驱动的，提交那一刻才看得见
        return out, captured

    def _source_ok(self, name="i2isrc_x.png"):
        return (
            mock.patch.object(comfy_src, "resolve",
                              lambda spec: (b"RAW", "233 发的图")),
            mock.patch.object(comfy_src, "fit",
                              lambda raw, **kw: (b"FIT", (1216, 1216))),
            mock.patch.object(comfy_src, "upload", lambda raw, **kw: name),
        )


class I2IFlowTest(_I2IRunner, unittest.TestCase):
    """图生图整体停用：**任何**渠道带 source_image 都当场拒，绝不静默退化。

    用户的问题不是「画得不好」，是模型一看见引用图就往改图上想（见
    generate_image 里 `_I2I_SKILLS` 的注释）。所以这里测的不是「哪个渠道
    不支持」，而是「**这个动作本身已经没有入口了**」。
    """

    def test_default_call_is_still_text2img(self):
        out, wf = self._run(prompt="1girl, solo")
        self.assertIn("已经在画了", out)
        self.assertEqual(wf["9"]["class_type"], "EmptyLatentImage")
        self.assertNotIn("24", wf)
        self.assertEqual(wf["4"]["inputs"]["text"], "@kibro, 1girl, solo")

    def test_source_image_is_refused_whatever_the_skill(self):
        """点了名也一样拒——停用的是「图生图」，不是「某个渠道的图生图」。"""
        for skill in ("anima", "image_gen_v1", "krea2"):
            out, wf = self._run(prompt="x", skill=skill, source_image="1")
            self.assertIn("source_image", out)
            self.assertEqual(wf, {}, skill)

    def test_refusal_points_at_the_right_thing_to_do(self):
        """真正的痛点在这儿：模型得知道「引用图 = 看得见」而不是「要改图」。"""
        out, _ = self._run(prompt="x", source_image="1")
        self.assertIn("看得见", out)
        self.assertIn("anima", out)             # 明确给出该走的渠道
        self.assertIn("反推", out)               # 用户要的正是「反推提示词」
        self.assertIn("别跟对方解释技术原因", out)

    def test_refusal_never_mentions_a_channel_name(self):
        """拒收时不能把 qwen_image_v1 这种名字甩出去——群里看到很奇怪。"""
        out, _ = self._run(prompt="x", source_image="1")
        self.assertNotIn("qwen", out)

    def test_nothing_reaches_comfyui(self):
        """拦在 skill 解析之前：一次白跑都没有。"""
        out, wf = self._run(prompt="x", source_image="1")
        self.assertEqual(wf, {})


@_qwen_off
class QwenI2ITest(_I2IRunner, unittest.TestCase):
    """qwen 图生图：唯一渠道、按指令改、不吃 denoise。

    走的是「参考图直进文本编码器」那条路，跟已下线的 anima 垫图链
    （LoadImage→VAEEncode）完全不同。
    """

    def _qwen(self, **kw):
        p1, p2, p3 = self._source_ok()
        with p1, p2, p3:
            return self._run(**kw)

    def test_source_image_alone_defaults_to_qwen(self):
        """没点名 skill + 给了源图 → 走 qwen，不是 anima。这是用户要的默认。"""
        out, wf = self._qwen(prompt="把衣服换成红色卫衣", source_image="1")
        self.assertIn("已经在画了", out)
        self.assertIn("233 发的图", out)
        self.assertEqual(wf["20"]["class_type"], "TextEncodeQwenImage21")
        # qwen 走的是「参考图直进文本编码器」，不是 anima 的 VAEEncode 垫图链
        self.assertEqual(wf["13"]["class_type"], "LoadImage")
        self.assertEqual(wf["13"]["inputs"]["image"], "i2isrc_x.png")
        self.assertNotIn("25", wf)

    def test_source_image_reaches_the_encoder(self):
        _, wf = self._qwen(prompt="换成夜景", source_image="1")
        self.assertEqual(wf["20"]["inputs"]["images.image_1"], ["13", 0])
        self.assertEqual(wf["20"]["inputs"]["vae"], ["12", 0])
        self.assertIn("换成夜景", wf["20"]["inputs"]["prompt"])

    def test_latent_comes_from_the_encoder_not_empty_latent(self):
        """TextEncodeQwenImage21 第三个输出就是 latent——别再挂 EmptyLatentImage。"""
        _, wf = self._qwen(prompt="x", source_image="1")
        self.assertEqual(wf["30"]["inputs"]["latent_image"], ["20", 2])
        self.assertNotIn("EmptyLatentImage",
                         [n.get("class_type") for n in wf.values()])

    def test_denoise_is_pinned_to_one(self):
        """编辑模型没有「保留多少原图」这个旋钮，模型传了也不认。"""
        _, wf = self._qwen(prompt="x", source_image="1", denoise=0.35)
        self.assertEqual(wf["30"]["inputs"]["denoise"], 1.0)

    def test_junk_denoise_is_ignored_not_fatal(self):
        """垫图重绘下线后 denoise 已无渠道消费，写错也不该让 qwen 失败。"""
        out, wf = self._qwen(prompt="x", source_image="1", denoise="随便")
        self.assertIn("已经在画了", out)
        self.assertEqual(wf["30"]["inputs"]["denoise"], 1.0)

    def test_explicit_qwen_still_works(self):
        _, wf = self._qwen(prompt="x", skill="qwen_image_v1", source_image="1")
        self.assertEqual(wf["20"]["class_type"], "TextEncodeQwenImage21")

    def test_qwen_text2img_workflow_untouched(self):
        """点名 qwen 但没给源图 → 还是文生图那份，别误切到 i2i。"""
        out, wf = self._run(prompt="a cat", skill="qwen_image_v1")
        self.assertIn("已经在画了", out)
        self.assertNotIn("TextEncodeQwenImage21",
                         [n.get("class_type") for n in wf.values()])


class NaiI2ITest(unittest.TestCase):
    """NAI 图生图：引用图 → base64 入队快照，复用 NAI 三层闸（不新增开关）。

    NAI 分流发生在 ComfyUI i2i 拒收**之前**（generate_image 的 nai 分支），
    所以这里的 source_image 不吃「图生图整体停用」的闭门羹。
    """

    KEY = ("group", 999001)

    def _run(self, allowed=True, context=None,
             resolve_result=(b"RAW", "引用的那张图"), **kw):
        """跑 NAI 分支。覆写一律走参数，**不要在测试体里再叠 patch**——
        _run 的 addCleanup 晚于外层 with 恢复，会把外层 patch 的值泄漏给
        后面的用例（真实撞过：no_quote 泄漏进了 ResolveTest）。"""
        captured = {}
        fake_job = mock.Mock()

        def fake_enqueue(target, target_id, workflow, skill=None, nai_i2i=None):
            captured.update({"target": target, "target_id": target_id,
                             "workflow": workflow, "skill": skill,
                             "nai_i2i": nai_i2i})
            return fake_job, None

        def fake_resolve(spec):
            if isinstance(resolve_result, Exception):
                raise resolve_result
            return resolve_result

        why = "" if allowed else "NAI 未在本 agent 启用（管理页全局开关未开）"
        for patcher in (
            mock.patch.object(gi, "_qq_gate", lambda: None),
            mock.patch.object(gi.image_jobs, "enqueue", fake_enqueue),
            mock.patch.object(gi.image_jobs, "ahead_of", lambda job: 0),
            mock.patch.object(gi, "is_cancelled", lambda: False),
            mock.patch.object(qq_api, "current_context",
                              lambda: self.KEY if context is None else context),
            mock.patch.object(agents, "nai_allowed",
                              lambda *a, **k: (allowed, why)),
            mock.patch.object(comfy_src, "resolve", fake_resolve),
            mock.patch.object(nai, "prepare_image",
                              lambda raw: "QUJD-B64"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        out = gi._generate_image(**kw)
        return out, captured

    def test_i2i_enqueues_snapshotted_image(self):
        out, cap = self._run(prompt="make it night", skill="nai",
                             source_image="1")
        self.assertIn("垫的是引用的那张图", out)
        self.assertIn("不要输出图片地址", out)
        self.assertEqual(cap["skill"], "nai")
        self.assertEqual(cap["workflow"], "make it night")
        self.assertEqual(cap["nai_i2i"]["image"], "QUJD-B64")
        self.assertEqual(cap["nai_i2i"]["note"], "引用的那张图")

    def test_strength_from_denoise(self):
        _, cap = self._run(prompt="x", skill="nai", source_image="1",
                           denoise=0.35)
        self.assertEqual(cap["nai_i2i"]["strength"], 0.35)

    def test_junk_denoise_falls_back_to_default(self):
        _, cap = self._run(prompt="x", skill="nai", source_image="1",
                           denoise="随便")
        self.assertEqual(cap["nai_i2i"]["strength"], nai.NAI_I2I_STRENGTH)

    def test_out_of_range_denoise_is_clamped(self):
        _, cap = self._run(prompt="x", skill="nai", source_image="1",
                           denoise=5)
        self.assertEqual(cap["nai_i2i"]["strength"], 0.9)

    def test_text2img_still_has_no_snapshot(self):
        out, cap = self._run(prompt="a cat", skill="nai")
        self.assertIn("已经在画了", out)
        self.assertNotIn("垫的是", out)
        self.assertIsNone(cap["nai_i2i"])

    def test_without_quote_refuses_instead_of_degrading(self):
        """没引用就报错让对方引用，绝不悄悄退回文生图。"""
        out, cap = self._run(
            prompt="x", skill="nai", source_image="1",
            resolve_result=RuntimeError("没看到引用的图片，垫不了图。"))
        self.assertIn("引用", out)
        self.assertNotIn("已经在画", out)
        self.assertEqual(cap, {})

    def test_gate_refusal_covers_i2i_too(self):
        """NAI 闸没过，i2i 和 t2i 拒法一致——不新增开关。"""
        out, cap = self._run(allowed=False, prompt="x", skill="nai",
                             source_image="1")
        self.assertIn("未在本 agent 启用", out)
        self.assertEqual(cap, {})

    def test_web_is_refused(self):
        out, cap = self._run(context=(None, None), prompt="x", skill="nai",
                             source_image="1")
        self.assertIn("仅支持 QQ", out)
        self.assertEqual(cap, {})


if __name__ == "__main__":
    unittest.main()
