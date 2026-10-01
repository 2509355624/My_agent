"""图生图：源图解析 / 缩放 / 上传，以及工具里的分流与占位符注入。

零网络、零落盘：图片下载与 ComfyUI 上传全部 mock 掉。这里唯一碰磁盘的是
「本地路径」那两条用例——它们故意拿本测试文件自己当那张图。

图生图 2026-10-01 重开：常规档 `anima_*` + 高清快档 `hd_fast_*` + 高清二档
`hd_2_*`，共 12 个渠道（`hd_3_*` 不给）。**「什么时候才允许垫图」这条边界不在
这里测**——它写在工具描述里（见 test_image_lora 的 description 用例）。这里只管
「模型传了 source_image 之后，工作流到底怎么走」。
"""

import io
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

from app import agents, comfy_src, nai, qq_api, stickers, vision
from app.skills import load_workflow
from app.tools.normal import generate_image as gi

SKILLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "skills")


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

    def test_custom_long_side(self):
        """高清档按自己的画布缩（长边 1536），不是恒定的 1216。

        小图也照样放大到画布长边——垫图出来的尺寸就等于这一步的尺寸。
        """
        _, (w, h) = comfy_src.fit(_png(512, 768), max_side=1536)
        self.assertEqual((w, h), (1024, 1536))

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


class I2IWorkflowShapeTest(unittest.TestCase):
    """12 份 `workflow_i2i.json` 的骨架形状。

    骨架由 `_make_i2i_workflows.py` 从同目录的文生图骨架生成，**很容易漏跑一个
    渠道**——漏了那个渠道垫图会当场报「没有图生图工作流」。这里逐份验结构，
    不认任何具体节点编号（每个渠道的 id 都不一样）。

    只读文件，不碰网络。
    """

    def _load(self, skill):
        wf = load_workflow(os.path.join(SKILLS, skill, "workflow_i2i.json"))
        self.assertIsNotNone(wf, "%s 缺 workflow_i2i.json" % skill)
        return wf

    def test_all_twelve_have_the_i2i_shape(self):
        self.assertEqual(len(gi._I2I_SKILLS), 12)
        for skill in gi._I2I_SKILLS:
            with self.subTest(skill=skill):
                wf = self._load(skill)
                kinds = [n["class_type"] for n in wf.values()]
                # 图生图的全部差别就在 latent 从哪来：空 latent 换成编码源图
                self.assertNotIn("EmptyLatentImage", kinds)
                self.assertEqual(kinds.count("LoadImage"), 1)
                self.assertEqual(kinds.count("VAEEncode"), 1)
                # 采样/放大那一整套照抄文生图，一样不少
                self.assertEqual(kinds.count("KSampler"), 2)
                self.assertEqual(kinds.count("LatentUpscaleBy"), 1)

    def test_the_two_placeholders_are_there_to_be_filled(self):
        """运行时只替换这两个占位符，骨架里必须原样留着它们。"""
        for skill in gi._I2I_SKILLS:
            with self.subTest(skill=skill):
                wf = self._load(skill)
                self.assertTrue(any(
                    (n.get("inputs") or {}).get("image") == "__SOURCE_IMAGE__"
                    for n in wf.values()), "LoadImage 没留 __SOURCE_IMAGE__")
                self.assertTrue(any(
                    (n.get("inputs") or {}).get("denoise") == "__DENOISE__"
                    for n in wf.values()), "一段 KSampler 没留 __DENOISE__")

    def test_the_encoder_feeds_the_first_stage(self):
        """一段必须吃 VAEEncode 出来的 latent——接错了就等于没垫图。"""
        for skill in gi._I2I_SKILLS:
            with self.subTest(skill=skill):
                wf = self._load(skill)
                enc = [nid for nid, n in wf.items()
                       if n["class_type"] == "VAEEncode"][0]
                stage1 = [nid for nid, n in wf.items()
                          if n["class_type"] == "KSampler"
                          and n["inputs"].get("latent_image") == [enc, 0]]
                self.assertEqual(len(stage1), 1,
                                 "没有唯一的一段 KSampler 吃 VAEEncode 的 latent")

    def test_hd_3_has_no_i2i_workflow_at_all(self):
        """三档不给图生图：白名单排除了它，连骨架都不该存在（免得两处各说各话）。"""
        for style in ("clear", "soft", "gloss", "curvy"):
            skill = "hd_3_" + style
            self.assertTrue(os.path.isdir(os.path.join(SKILLS, skill)),
                            "文生图渠道 %s 应该还在" % skill)
            self.assertFalse(
                os.path.exists(os.path.join(SKILLS, skill, "workflow_i2i.json")),
                "%s 不该有图生图骨架" % skill)


class _I2IRunner(object):
    """跑 generate_image 本体，拦住提交那一刻看它到底送了什么。

    故意不是 TestCase——两个 i2i 用例类（本机 12 渠道 / NAI 云端）共用这套拦截，
    直接继承 TestCase 的话基类的用例会在子类里再跑一遍。
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

    def _feed(self, name="i2isrc_x.png", seen=None):
        """把「取图 → 缩放 → 上传」三步全换成假的。

        `seen` 非 None 时顺手记下 fit 的 max_side——那是源图被缩到多少的凭据。
        """
        def fake_fit(raw, max_side=None):
            if seen is not None:
                seen["max_side"] = max_side
            return b"FIT", (max_side or comfy_src.MAX_SIDE, 8)

        return (
            mock.patch.object(comfy_src, "resolve",
                              lambda spec: (b"RAW", "引用的那张图")),
            mock.patch.object(comfy_src, "fit", fake_fit),
            mock.patch.object(comfy_src, "upload", lambda raw: name),
        )


class I2IFlowTest(_I2IRunner, unittest.TestCase):
    """本机 12 渠道的图生图：换骨架、填占位符、按本档画布缩源图。

    出图尺寸 = 「源图缩到本档画布长边」再乘二段放大倍率，**跟源图原始尺寸无关**。
    所以「高清档垫图」出来还是高清档那个尺寸——这正是 `hd_fast_*` / `hd_2_*`
    能被垫的理由，也是 `hd_3_*` 被排除的理由（它自己就慢）。
    """

    def _kinds(self, wf):
        return {n["class_type"] for n in wf.values()}

    def _one(self, wf, class_type):
        hits = [nid for nid, n in wf.items() if n["class_type"] == class_type]
        self.assertEqual(len(hits), 1, "预期恰好一个 %s，实际 %s"
                         % (class_type, hits))
        return hits[0]

    def test_default_call_is_still_text2img(self):
        """不传 source_image 就**一行图生图逻辑都不走**——文生图原样不动。"""
        out, wf = self._run(prompt="1girl, solo")
        self.assertIn("已经在画了", out)
        kinds = self._kinds(wf)
        self.assertIn("EmptyLatentImage", kinds)
        self.assertNotIn("LoadImage", kinds)
        self.assertNotIn("VAEEncode", kinds)
        self.assertNotIn("垫的是", out)
        texts = [n["inputs"].get("text") for n in wf.values()
                 if n["class_type"] == "CLIPTextEncode"]
        self.assertIn("@kibro, 1girl, solo", texts)   # 模板前缀没被弄丢

    def test_source_image_switches_to_i2i(self):
        """给了源图：骨架换成 LoadImage → VAEEncode → 一段采样。"""
        p1, p2, p3 = self._feed()
        with p1, p2, p3:
            out, wf = self._run(prompt="把衣服换成红色", source_image="1")
        self.assertIn("已经在画了", out)
        self.assertIn("垫的是引用的那张图", out)      # 垫了哪张要回给模型，免得它说错
        kinds = self._kinds(wf)
        self.assertIn("LoadImage", kinds)
        self.assertIn("VAEEncode", kinds)
        self.assertNotIn("EmptyLatentImage", kinds)
        load = self._one(wf, "LoadImage")
        self.assertEqual(wf[load]["inputs"]["image"], "i2isrc_x.png")
        enc = self._one(wf, "VAEEncode")
        self.assertEqual(wf[enc]["inputs"]["pixels"], [load, 0])

    def test_the_first_stage_takes_the_encoded_source(self):
        """一段的 latent 来自编码后的源图，denoise 定死 0.6（不给模型调）。"""
        p1, p2, p3 = self._feed()
        with p1, p2, p3:
            _, wf = self._run(prompt="x", source_image="1")
        enc = self._one(wf, "VAEEncode")
        stage1 = [nid for nid, n in wf.items()
                  if n["class_type"] == "KSampler"
                  and n["inputs"].get("latent_image") == [enc, 0]]
        self.assertEqual(len(stage1), 1)
        self.assertEqual(wf[stage1[0]]["inputs"]["denoise"], gi.I2I_DENOISE)

    def test_denoise_from_the_model_is_ignored(self):
        """模型传了 denoise 也不认——它一调就会以为「调低 = 只微调」。"""
        p1, p2, p3 = self._feed()
        with p1, p2, p3:
            out, wf = self._run(prompt="x", source_image="1", denoise=0.2)
        self.assertIn("已经在画了", out)
        enc = self._one(wf, "VAEEncode")
        stage1 = [nid for nid, n in wf.items()
                  if n["class_type"] == "KSampler"
                  and n["inputs"].get("latent_image") == [enc, 0]][0]
        self.assertEqual(wf[stage1]["inputs"]["denoise"], gi.I2I_DENOISE)

    def test_source_is_scaled_to_the_channels_own_canvas(self):
        """源图缩到**本档画布的长边**，不是 comfy_src 默认的 1216。

        缩错了高清档就名不副实：垫一张小图出来还是小图。
        """
        for skill, expect in (("anima_clear", 1024),    # 728×1024
                              ("hd_fast_clear", 1536),  # 1024×1536
                              ("hd_2_gloss", 1536)):    # 1024×1536
            with self.subTest(skill=skill):
                seen = {}
                p1, p2, p3 = self._feed(seen=seen)
                with p1, p2, p3:
                    out, _ = self._run(prompt="x", skill=skill, source_image="1")
                self.assertIn("已经在画了", out)
                self.assertEqual(seen["max_side"], expect, skill)

    def test_every_supported_tier_can_be_fed(self):
        """常规档 / 高清快档 / 高清二档都能垫——12 个渠道一个都不许漏。"""
        for skill in ("anima_soft", "anima_curvy", "hd_fast_gloss",
                      "hd_2_clear", "hd_2_curvy"):
            with self.subTest(skill=skill):
                p1, p2, p3 = self._feed()
                with p1, p2, p3:
                    out, wf = self._run(prompt="x", skill=skill,
                                        source_image="1")
                self.assertIn("已经在画了", out)
                self.assertIn("VAEEncode", self._kinds(wf))

    def test_second_stage_upscale_survives(self):
        """二段放大照抄不动——高清二档垫图出来还是 1.3× 那个尺寸。"""
        p1, p2, p3 = self._feed()
        with p1, p2, p3:
            _, wf = self._run(prompt="x", skill="hd_2_clear", source_image="1")
        up = self._one(wf, "LatentUpscaleBy")
        self.assertEqual(wf[up]["inputs"]["scale_by"], 1.3)

    def test_hd_3_is_refused_and_touches_nothing(self):
        """三档不给图生图：点名也一样拒，一步都不往 ComfyUI 送。"""
        out, wf = self._run(prompt="x", skill="hd_3_clear", source_image="1")
        self.assertIn("不支持图生图", out)
        self.assertEqual(wf, {})

    def test_source_failure_reports_and_submits_nothing(self):
        """取不到源图就报错，**绝不悄悄退回文生图**。

        对方以为改的是自己那张，收到的却是凭空画的，比直接报错糟得多。
        """
        with mock.patch.object(comfy_src, "resolve",
                               side_effect=RuntimeError("这儿没有图")):
            out, wf = self._run(prompt="x", source_image="1")
        self.assertEqual(out, "这儿没有图")
        self.assertEqual(wf, {})

    def test_upload_failure_reports_and_submits_nothing(self):
        p1, p2, _ = self._feed()
        with p1, p2, mock.patch.object(comfy_src, "upload",
                                       side_effect=RuntimeError("传不进去")):
            out, wf = self._run(prompt="x", source_image="1")
        self.assertEqual(out, "传不进去")
        self.assertEqual(wf, {})

    def test_blank_source_image_is_not_i2i(self):
        """空串等于没传（`if str(source_image or "").strip()`）——别把它当垫图。"""
        out, wf = self._run(prompt="x", source_image="")
        self.assertIn("已经在画了", out)
        self.assertIn("EmptyLatentImage", self._kinds(wf))


class NaiI2ITest(unittest.TestCase):
    """NAI 图生图：引用图 → base64 入队快照，复用 NAI 三层闸（不新增开关）。

    NAI 走自己的云分支（generate_image 里的 nai 分流在 ComfyUI 那套之前），
    所以这里的 source_image 与本机渠道的 `_I2I_SKILLS` 白名单无关。
    """

    KEY = ("group", 999001)

    def _run(self, allowed=True, context=None,
             resolve_result=(b"RAW", "引用的那张图"), **kw):
        """跑 NAI 分支。覆写一律走参数，**不要在测试体里再叠 patch**——
        _run 的 addCleanup 晚于外层 with 恢复，会把外层 patch 的值泄漏给
        后面的用例（真实撞过：no_quote 泄漏进了 ResolveTest）。"""
        captured = {}
        fake_job = mock.Mock()

        def fake_enqueue(target, target_id, workflow, skill=None, nai_i2i=None,
                         prompt=None, intent=None):
            captured.update({"target": target, "target_id": target_id,
                             "workflow": workflow, "skill": skill,
                             "nai_i2i": nai_i2i, "intent": intent})
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
        # NAI 也要带意图指纹，否则同一个会话里连点两次会排出两张一样的图
        # （见 image_jobs.find_pending_duplicate）。
        self.assertTrue(cap["intent"])

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
