"""跑图台横屏 + ComfyUI 重启后取图（2026-11-10）测试。

覆盖三块：
1. `image_jobs.set_canvas_size` 只改画布、放大链原样保留（「正常 2x 放大」）；
2. 跑图台「横屏」开关端到端：提交 → worker 改工作流 → 账本，以及和手填
   尺寸互斥、NAI 忽略；
3. `/api/gen/img` 先读本机磁盘、磁盘没有才代理 `/view`（重启空窗里也能看图）。

回归锁两处容易再犯的错：横屏分支不能把 moss 的手填尺寸 `apply_size` 吃掉；
`comfy_out.local_output_file` 与 `main._local_output_file` 不能分成两套实现。
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock

from app import comic, comfy_out, gen_api, image_jobs, main

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _wf():
    """最小但形状真实的工作流：画布 + 1.2× latent + 2x 超分 + 采样器。"""
    return {
        "9": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 1024, "height": 1536, "batch_size": 1}},
        "20": {"class_type": "LatentUpscaleBy",
               "inputs": {"scale_by": 1.2, "samples": ["9", 0]}},
        "30": {"class_type": "UpscaleModelLoader",
               "inputs": {"model_name": "2x_Ani4Kv2_G6i2_Compact_107500.pth"}},
        "40": {"class_type": "KSampler",
               "inputs": {"steps": 15, "cfg": 1.0}},
    }


def _wait_done(jid, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with gen_api._JOBS_LOCK:
            if gen_api._JOBS[jid]["state"] in ("done", "error", "cancelled"):
                return True
        time.sleep(0.02)
    return False


class SetCanvasSizeTest(unittest.TestCase):
    """和 QQ 横屏共用 `_find_size_node`，只动画布。"""

    def test_only_canvas_changes_upscale_chain_untouched(self):
        wf = _wf()
        self.assertTrue(image_jobs.set_canvas_size(wf, 1920, 1080))
        self.assertEqual((wf["9"]["inputs"]["width"],
                          wf["9"]["inputs"]["height"]), (1920, 1080))
        self.assertEqual(wf["20"]["inputs"]["scale_by"], 1.2)
        self.assertIn("2x_", wf["30"]["inputs"]["model_name"])
        self.assertEqual(wf["40"]["inputs"]["steps"], 15)

    def test_hires_pair_is_synced(self):
        wf = _wf()
        wf["9"]["inputs"].update({"enable_hires": True,
                                  "hires_width": 1024, "hires_height": 1536})
        self.assertTrue(image_jobs.set_canvas_size(wf, 1920, 1080))
        self.assertEqual((wf["9"]["inputs"]["hires_width"],
                          wf["9"]["inputs"]["hires_height"]), (1920, 1080))

    def test_recognizes_batch_and_flux2_canvas(self):
        """`landscape_workflow` 认得的那几种画布，这里也要认得。"""
        for ct in ("BatchPromptImageGenerator",
                   "DualModelBatchPromptImageGenerator",
                   "EmptyFlux2LatentImage"):
            wf = {"7": {"class_type": ct,
                        "inputs": {"width": 1024, "height": 1536}}}
            self.assertTrue(image_jobs.set_canvas_size(wf, 1920, 1080), ct)
            self.assertEqual(wf["7"]["inputs"]["width"], 1920, ct)

    def test_missing_canvas_returns_false(self):
        self.assertFalse(image_jobs.set_canvas_size(
            {"1": {"class_type": "KSampler", "inputs": {"steps": 8}}},
            1920, 1080))
        self.assertFalse(image_jobs.set_canvas_size(None, 1920, 1080))


class SubmitLandscapeTest(unittest.TestCase):
    """提交 → worker → 账本，全程不碰真实 ComfyUI。"""

    def setUp(self):
        self.c = main.app.test_client()
        self.seen = {}
        self.jids = []

    def tearDown(self):
        with gen_api._JOBS_LOCK:
            for jid in self.jids:
                job = gen_api._JOBS.get(jid)
                if job and job["state"] == "queued":
                    job["state"] = "done"

    def _fake_submit(self):
        def f(wf, skill, prompt, seed, **kw):
            self.seen["wf"] = wf
            return ("x.png", {"status": {"messages": []}})
        return f

    def _submit(self, **kw):
        body = {"skill": "silver", "prompt": "1girl, solo"}
        body.update(kw)
        with mock.patch.object(gen_api, "generate_image_build",
                               side_effect=lambda sk, p, s: _wf()), \
             mock.patch.object(comic, "_submit", side_effect=self._fake_submit()), \
             mock.patch.object(comic, "_submit_retry",
                               side_effect=lambda fn, what: fn()):
            r = self.c.post("/api/gen/submit", json=body)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            jid = r.get_json()["job"]["id"]
            self.jids.append(jid)
            self.assertTrue(_wait_done(jid), "worker 没跑完")
        return jid

    def _row(self, jid):
        return next(r for r in gen_api._ledger_rows() if r["tag"] == jid)

    def _size(self):
        ins = self.seen["wf"]["9"]["inputs"]
        return ins["width"], ins["height"]

    def test_landscape_sets_1920x1080_and_keeps_2x(self):
        jid = self._submit(landscape=True)
        self.assertEqual(self._size(), (1920, 1080))
        self.assertEqual(self.seen["wf"]["20"]["inputs"]["scale_by"], 1.2)
        self.assertIn("2x_", self.seen["wf"]["30"]["inputs"]["model_name"])

    def test_landscape_is_recorded_in_ledger(self):
        row = self._row(self._submit(landscape=True))
        self.assertTrue(row["landscape"])
        self.assertEqual(row["size"], "1920×1080 横屏")

    def test_not_checked_keeps_template_canvas(self):
        jid = self._submit()
        self.assertEqual(self._size(), (1024, 1536))
        self.assertNotIn("landscape", self._row(jid))

    def test_manual_size_still_works(self):
        """回归：横屏分支不能把 moss 的手填尺寸 apply_size 一起吃掉。"""
        row = self._row(self._submit(skill="moss-qunyou",
                                    width=1536, height=2048))
        self.assertEqual(self._size(), (1536, 2048))
        self.assertIn("1536", row["size"])

    def test_landscape_wins_over_manual_size(self):
        """两者都改同一个画布节点，提交时手填尺寸必须让位。"""
        jid = self._submit(skill="moss-qunyou", width=1536, height=2048,
                           landscape=True)
        self.assertEqual(self._size(), (1920, 1080))
        with gen_api._JOBS_LOCK:
            self.assertIsNone(gen_api._JOBS[jid]["width"])

    def test_nai_ignores_landscape(self):
        fake = mock.Mock()
        fake.wait.return_value = {"images": ["C:/tmp/a.png"]}
        with mock.patch.object(image_jobs, "enqueue",
                               return_value=(fake, None)):
            r = self.c.post("/api/gen/submit",
                            json={"skill": "nai", "prompt": "1girl",
                                  "landscape": True})
            self.assertEqual(r.status_code, 200)
            jid = r.get_json()["job"]["id"]
            self.jids.append(jid)
            self.assertTrue(_wait_done(jid))
        with gen_api._JOBS_LOCK:
            self.assertFalse(gen_api._JOBS[jid]["landscape"])


class LocalOutputFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gen_landscape_out_")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmp, ignore_errors=True))

    def _put(self, rel, data=PNG):
        p = os.path.join(self.tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_real_file_and_subfolder(self):
        p = self._put("web/生图页/silver/a.png")
        self.assertEqual(comfy_out.local_output_file(
            "web/生图页/silver/a.png", root=self.tmp), p)

    def test_missing_and_unset_root(self):
        self.assertIsNone(comfy_out.local_output_file("nope.png", root=self.tmp))
        self.assertIsNone(comfy_out.local_output_file("nope.png", root=""))

    def test_traversal_is_rejected(self):
        outside = os.path.join(os.path.dirname(self.tmp), "secret.png")
        with open(outside, "wb") as f:
            f.write(PNG)
        self.addCleanup(lambda: os.path.exists(outside) and os.remove(outside))
        for rel in ("../secret.png", "web/../../secret.png",
                    "C:/Windows/win.ini", "..\\secret.png"):
            self.assertIsNone(comfy_out.local_output_file(rel, root=self.tmp), rel)

    def test_main_alias_uses_same_resolver(self):
        """main 侧仍可 patch 自己的模块级变量，实现不能分家。"""
        p = self._put("a.png")
        with mock.patch.object(main, "COMFYUI_OUTPUT_DIR", self.tmp):
            self.assertEqual(main._local_output_file("a.png"), p)
        self.assertIsNone(main._local_output_file("../x.png"))


class ApiImgTest(unittest.TestCase):
    """`/api/gen/img`：图在本机磁盘上就不该依赖 ComfyUI 在线。"""

    def setUp(self):
        self.c = main.app.test_client()
        self.tmp = tempfile.mkdtemp(prefix="gen_landscape_img_")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmp, ignore_errors=True))
        p = os.path.join(self.tmp, "web", "gen")
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "a.png"), "wb") as f:
            f.write(PNG)

    def test_local_file_is_served_without_view(self):
        with mock.patch("app.config.COMFYUI_OUTPUT_DIR", self.tmp), \
             mock.patch.object(gen_api._session, "get") as get:
            r = self.c.get("/api/gen/img?f=web/gen/a.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, PNG)
        get.assert_not_called()

    def test_missing_file_still_falls_back_to_view(self):
        with mock.patch("app.config.COMFYUI_OUTPUT_DIR", self.tmp), \
             mock.patch.object(gen_api._session, "get") as get:
            get.return_value = mock.Mock(status_code=200, content=b"REMOTE")
            r = self.c.get("/api/gen/img?f=web/gen/missing.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"REMOTE")
        get.assert_called_once()

    def test_path_traversal_is_400(self):
        r = self.c.get("/api/gen/img?f=../secret.png")
        self.assertEqual(r.status_code, 400)


class GenPadPageTest(unittest.TestCase):
    """前端静态锁：开关、提交字段、重试函数都在，且不能重名覆盖。"""

    def test_landscape_and_retry_are_wired(self):
        path = os.path.join(gen_api.WEB_DIR, "gen_pad.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        self.assertIn('id="landscapeOn"', html)
        self.assertIn("body.landscape = true", html)
        self.assertIn("landOn.disabled = !!cloud", html)
        self.assertEqual(html.count("function loadImg("), 1)
        self.assertIn("r=1", html)
        self.assertEqual(html.count("loadImg(img, '/api/gen/img"), 2)


if __name__ == "__main__":
    unittest.main()
