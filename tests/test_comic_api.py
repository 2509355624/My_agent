# -*- coding: utf-8 -*-
"""漫画 API 测试：编剧改异步后的 job 轮询、参数透传与失败上报。

2026-10-11 加。背景：编剧从「单次 API 同步等待」换成「Agent 查库 + 后台线程」，
`POST /api/comic/script` 不再直接返回剧本，而是返回 job 让前端轮询
`/api/comic/script_status`。这份测试钉死：

  1. 立刻返回 job（不阻塞请求线程——Flask 是单线程跑的，同步等待会把
    同一个页面的槽位轮询一起堵死）;
  2. 完成后 job 里带 task 名与剧本文本，且 script.md 真落盘;
  3. provider / model 从前端透传到编剧 Agent（漫画页的模型下拉就靠这条）;
  4. 编剧抛异常时状态是 error 而不是永远 running。
"""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_agent, comic_api, main

GOOD_SCRIPT = "# 标题：测试\n\n## 01\n画面: 1girl, solo\n气泡: 一\n"


class ComicScriptApiTest(unittest.TestCase):

    def setUp(self):
        self.c = main.app.test_client()
        self.tmp = tempfile.mkdtemp(prefix="comic_api_")
        with comic_api._SCRIPT_LOCK:
            comic_api._SCRIPT_JOBS.clear()
        # 任务目录钉到临时目录：绝不能让测试往真实 comics/ 里写东西
        self._patch = mock.patch.object(comic, "_next_task_dir",
                                        side_effect=self._mk_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        with comic_api._SCRIPT_LOCK:
            comic_api._SCRIPT_JOBS.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk_dir(self, root=None):
        return os.path.join(self.tmp, "C20261011-01")

    def _wait(self, job, timeout=5.0):
        """轮询到任务结束（测试里 write_script 是 mock 的，所以很快）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.c.get("/api/comic/script_status?job=" + job).get_json()
            if d.get("state") != "running":
                return d
            time.sleep(0.05)
        self.fail("编剧任务超时未完成")

    def _post(self, **body):
        body.setdefault("brief", "公交站被抓包")
        r = self.c.post("/api/comic/script", json=body)
        return r.get_json()

    def test_missing_brief_is_400(self):
        r = self.c.post("/api/comic/script", json={"brief": "  "})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_unknown_job_is_404(self):
        r = self.c.get("/api/comic/script_status?job=nope")
        self.assertEqual(r.status_code, 404)

    def test_returns_job_and_finishes_with_script(self):
        with mock.patch.object(comic_agent, "write_script",
                               return_value=GOOD_SCRIPT):
            d = self._post(panels=1)
            self.assertTrue(d["ok"])
            self.assertTrue(d["job"])
            out = self._wait(d["job"])
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["script"], GOOD_SCRIPT)
        self.assertEqual(out["task"], "C20261011-01")
        # 剧本必须真落盘（后面的渲染阶段读的就是这个文件）
        with open(os.path.join(self.tmp, "C20261011-01", "script.md"),
                  encoding="utf-8") as f:
            self.assertIn("画面: 1girl", f.read())

    def test_provider_and_model_forwarded(self):
        with mock.patch.object(comic_agent, "write_script",
                               return_value=GOOD_SCRIPT) as m:
            d = self._post(provider="deepseek", model="deepseek-v4-flash")
            self._wait(d["job"])
        kw = m.call_args.kwargs
        self.assertEqual(kw.get("provider"), "deepseek")
        self.assertEqual(kw.get("model"), "deepseek-v4-flash")

    def test_empty_provider_stays_none(self):
        with mock.patch.object(comic_agent, "write_script",
                               return_value=GOOD_SCRIPT) as m:
            d = self._post(provider="", model="")
            self._wait(d["job"])
        kw = m.call_args.kwargs
        self.assertIsNone(kw.get("provider"))
        self.assertIsNone(kw.get("model"))

    def test_failure_reported_as_error(self):
        with mock.patch.object(comic_agent, "write_script",
                               side_effect=RuntimeError("连写 2 次都不合格")):
            d = self._post()
            out = self._wait(d["job"])
        self.assertEqual(out["state"], "error")
        self.assertIn("不合格", out["error"])

    def test_progress_log_surfaces(self):
        def fake(brief, panels=3, character="", provider=None, model=None,
                 progress=None, **kw):
            if progress:
                progress("search_tags(手)")
                progress("search_tags(眼泪)")
            return GOOD_SCRIPT

        with mock.patch.object(comic_agent, "write_script", side_effect=fake):
            d = self._post()
            out = self._wait(d["job"])
        self.assertEqual(out["state"], "done")
        self.assertIn("search_tags(眼泪)", out["log"])


class ComicManualApiTest(unittest.TestCase):
    """手动修复入口：/manual（手输指令跑 qwen）、/upload（顶替底图）、
    以及 /bubble 对**已完成**格开放（从底图重画＝改坏后的回退路）。

    2026-10-11 加。用户的原始诉求：「手动输入我的要求，然后手动进行 qwen 的
    调用来修复问题，不然现在结束了我也无法解决问题；以及手动添加原始的底图」。
    """

    SCRIPT = ("# 标题：t\n\n## 01\n画面: 1girl, solo\n气泡: 一\n标签1: 甲\n"
              "\n## 02\n画面: 1girl, solo\n气泡: 二\n标签1: 乙\n")
    NAME = "C20261011-01"

    def setUp(self):
        self.c = main.app.test_client()
        self.tmp = tempfile.mkdtemp(prefix="comic_manual_api_")
        # _task_dir 是拿 _COMICS_ROOT 拼的，不钉到临时目录就会去动真实 comics/
        self.patch_root = mock.patch.object(comic_api, "_COMICS_ROOT", self.tmp)
        self.patch_root.start()
        with comic_api._JOBS_LOCK:
            comic_api._JOBS.clear()
        self.dir = os.path.join(self.tmp, self.NAME)
        os.makedirs(os.path.join(self.dir, "raw"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "final"), exist_ok=True)
        with open(os.path.join(self.dir, "script.md"), "w", encoding="utf-8") as f:
            f.write(self.SCRIPT)
        meta, panels = comic.parse_script(os.path.join(self.dir, "script.md"))
        task = comic.Task(os.path.join(self.dir, "script.md"), meta, panels)
        task.panels[0].state = "done"           # 已完成是这次要打通的场景
        task.save_state()

    def tearDown(self):
        self.patch_root.stop()
        with comic_api._JOBS_LOCK:
            comic_api._JOBS.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with comic_api._JOBS_LOCK:
                j = dict(comic_api._JOBS.get(self.dir) or {})
            if not j.get("phase"):
                return j
            time.sleep(0.02)
        self.fail("后台任务超时未结束")

    def _load(self):
        return comic_api._load_task(self.dir)

    # ── /manual ────────────────────────────────────────

    def test_manual_rejects_empty_prompt(self):
        r = self.c.post("/api/comic/manual",
                        json={"task": self.NAME, "no": "01", "prompt": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertIn("改什么", r.get_json()["error"])

    def test_manual_unknown_panel_is_404(self):
        r = self.c.post("/api/comic/manual",
                        json={"task": self.NAME, "no": "09", "prompt": "改"})
        self.assertEqual(r.status_code, 404)

    def test_manual_passes_prompt_and_denoise(self):
        with mock.patch.object(comic, "render_manual") as m:
            r = self.c.post("/api/comic/manual",
                            json={"task": self.NAME, "no": "1", "prompt": "把字改清楚",
                                  "denoise": "0.35"})
            self.assertTrue(r.get_json()["ok"])
            self._idle()
        args = m.call_args.args
        self.assertEqual(args[1].no, "01")          # no 补零成两位
        self.assertEqual(args[2], "把字改清楚")
        self.assertEqual(args[3], "0.35")

    def test_manual_denoise_defaults_to_pipeline_value(self):
        with mock.patch.object(comic, "render_manual") as m:
            self.c.post("/api/comic/manual",
                        json={"task": self.NAME, "no": "01", "prompt": "改"})
            self._idle()
        self.assertEqual(m.call_args.args[3], comic.I2I_DENOISE)

    def test_manual_conflicts_with_a_running_job(self):
        with comic_api._JOBS_LOCK:
            comic_api._JOBS[self.dir] = {"phase": "bubble", "error": None}
        r = self.c.post("/api/comic/manual",
                        json={"task": self.NAME, "no": "01", "prompt": "改"})
        self.assertEqual(r.status_code, 409)

    def test_manual_error_lands_in_job_status(self):
        with mock.patch.object(comic, "render_manual",
                               side_effect=RuntimeError("ComfyUI 没在线")):
            self.c.post("/api/comic/manual",
                        json={"task": self.NAME, "no": "01", "prompt": "改"})
            j = self._idle()
        self.assertIn("ComfyUI", j.get("error") or "")

    # ── /bubble 对完成格开放 ────────────────────────────

    def test_bubble_accepts_done_panel_with_redo(self):
        with mock.patch.object(comic, "run") as m:
            r = self.c.post("/api/comic/bubble",
                            json={"task": self.NAME, "no": "01"})
            d = r.get_json()
            self.assertTrue(d["ok"])
            self.assertTrue(d["redo"])          # done 的格必须走 redo 才进得了阶段二
            self._idle()
        self.assertTrue(m.call_args.kwargs.get("redo"))

    def test_bubble_still_rejects_pending_panel(self):
        r = self.c.post("/api/comic/bubble",
                        json={"task": self.NAME, "no": "02"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("还没有场景图", r.get_json()["error"])

    # ── /upload ────────────────────────────────────────

    def test_upload_replaces_raw_and_drops_stale_final(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, "JPEG")   # 故意给 jpg
        buf.seek(0)
        task = self._load()
        with open(task.final_path(task.panels[0]), "wb") as f:
            f.write(b"OLDFINAL")
        r = self.c.post("/api/comic/upload",
                        data={"task": self.NAME, "no": "01",
                              "file": (buf, "base.jpg")},
                        content_type="multipart/form-data")
        self.assertTrue(r.get_json()["ok"], r.get_json())
        task = self._load()
        p = task.panels[0]
        # 统一转成真 PNG：.png 后缀配 jpg 字节会让 img 端点的 content-type 撒谎
        with open(task.raw_path(p), "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertEqual(p.state, "rendered")       # 换底图 → 旧的成品作废
        self.assertFalse(os.path.isfile(task.final_path(p)))

    def test_upload_rejects_non_image(self):
        r = self.c.post("/api/comic/upload",
                        data={"task": self.NAME, "no": "01",
                              "file": (io.BytesIO(b"not an image"), "x.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("读不出来", r.get_json()["error"])

    def test_upload_unknown_panel_is_404(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, "PNG")
        buf.seek(0)
        r = self.c.post("/api/comic/upload",
                        data={"task": self.NAME, "no": "09", "file": (buf, "x.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 404)

    # ── status 的 rev（图片指纹）────────────────────────

    def test_status_exposes_a_rev_per_panel(self):
        """每格要带 `rev`：前端逐格 diff 靠它发现「图被就地重写」。
        手动跑 Qwen 覆盖成品时 state/seed/url 全都不变，没 rev 就永远刷不出来
        （2026-10-11 用户报「重跑的结果没有给到界面」）。"""
        task = self._load()
        p = task.panels[0]
        os.makedirs(task.raw_dir, exist_ok=True)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        with open(task.final_path(p), "wb") as f:
            f.write(b"a")
        d = self.c.get("/api/comic/status?task=" + self.NAME).get_json()
        item = d["panels"][0]
        self.assertIn("rev", item)
        self.assertNotEqual(item["rev"], "0/0")     # 两张图都在，不该是空指纹

    def test_rev_changes_when_the_final_is_overwritten_in_place(self):
        """就地覆盖成品（字节数还一样）后 rev 必须变。

        同一秒内连跑两次是最常见的情形，所以指纹用毫秒 mtime——只比秒级时间
        或只比大小都可能判成「没变」，界面就会一直显示旧图。
        """
        task = self._load()
        p = task.panels[0]

        def rev():
            d = self.c.get("/api/comic/status?task=" + self.NAME).get_json()
            return d["panels"][0]["rev"]

        with open(task.final_path(p), "wb") as f:
            f.write(b"a")
        first = rev()
        time.sleep(0.01)
        with open(task.final_path(p), "wb") as f:
            f.write(b"b")                            # 同长度，只有时间戳变了
        self.assertNotEqual(first, rev())


if __name__ == "__main__":
    unittest.main()
