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

    def test_job_carries_fix_report_and_keeps_original_draft(self):
        """补正是编剧之后的**独立一步**：采纳的补正稿成为 script.md，
        编剧原稿另存 script_original.md，job 里带补正报告给页面显示。

        没有原稿就没法回退、也没法幂等重跑补正（重跑要从原稿重来）。
        """
        fixed = GOOD_SCRIPT.replace("画面: 1girl, solo",
                                    "画面: 1girl, solo, black hair, twintails")
        fake_fix = {"text": fixed, "issues": [], "adopted": True,
                    "diff": [{"no": "01", "changed": ["scene"],
                              "before": {"scene": "1girl, solo"},
                              "after": {"scene": "1girl, solo, black hair"}}],
                    "rejects": [], "warnings": [], "skipped": False, "model": ""}
        with mock.patch.object(comic_agent, "write_script",
                               return_value=GOOD_SCRIPT), \
                mock.patch.object(comic_agent, "fix_script",
                                  return_value=fake_fix) as fm:
            d = self._post()
            out = self._wait(d["job"])
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["script"], fixed)
        self.assertEqual(out["original"], GOOD_SCRIPT)
        self.assertTrue(out["fix"]["adopted"])
        self.assertEqual(out["fix"]["diff"][0]["no"], "01")
        # 补正报告要落盘（页面靠 status 读 fix.json，重启 Flask 也得还在）
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "C20261011-01", "fix.json")))
        # brief 也要落盘：手动重跑补正要拿它当「原始需求」
        with open(os.path.join(self.tmp, "C20261011-01", "brief.txt"),
                  encoding="utf-8") as f:
            self.assertIn("公交站被抓包", f.read())
        # 补正要拿到 brief（凭空人名判据靠它当白名单）
        self.assertEqual(fm.call_args.args[0], "公交站被抓包")

    def test_fix_failure_does_not_kill_the_script_job(self):
        """补正炸了不能让编剧任务失败——补正是加分项，剧本还在就该交出来。"""
        with mock.patch.object(comic_agent, "write_script",
                               return_value=GOOD_SCRIPT), \
                mock.patch.object(comic_agent, "fix_script",
                                  side_effect=RuntimeError("补正模型全挂了")):
            d = self._post()
            out = self._wait(d["job"])
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["script"], GOOD_SCRIPT)
        self.assertFalse(out["fix"]["adopted"])
        self.assertTrue(any("补正异常" in x for x in out["fix"]["rejects"]))


class ComicFixApiTest(unittest.TestCase):
    """内容补正的 HTTP 面：重跑（/fix）与取正文（/script_text）。

    2026-10-11 十轮加。两条都是踩出来的：
      - /fix 的底稿必须是 **script_original.md**，不是当前 script.md——拿补正过
        的稿再补一次，「未点名格不许动」的闸门会把上一轮成果当越权改动，diff
        也越滚越乱。
      - /script_text 存在的唯一理由是堵一个数据事故：补正就地改 script.md，
        而页面 textarea 里还是旧文本，用户接着点「确认开跑」会把旧文本存回去，
        补正结果整个作废。
    """

    DRAFT = "# 标题：t\n\n## 01\n画面: 1girl, twintails\n气泡: 一\n标签1: 甲\n"
    FIXED = "# 标题：t\n\n## 01\n画面: 1girl, black hair, twintails\n气泡: 一\n标签1: 甲\n"
    NAME = "C20261011-01"

    def setUp(self):
        self.c = main.app.test_client()
        self.tmp = tempfile.mkdtemp(prefix="comic_fix_api_")
        self.patch_root = mock.patch.object(comic_api, "_COMICS_ROOT", self.tmp)
        self.patch_root.start()
        with comic_api._JOBS_LOCK:
            comic_api._JOBS.clear()
        self.dir = os.path.join(self.tmp, self.NAME)
        os.makedirs(self.dir, exist_ok=True)
        self._write("script.md", self.FIXED)          # 当前稿＝补正过的
        self._write("script_original.md", self.DRAFT)  # 编剧原稿
        self._write("brief.txt", "妹妹离家出走\n")

    def tearDown(self):
        self.patch_root.stop()
        with comic_api._JOBS_LOCK:
            comic_api._JOBS.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def _idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with comic_api._JOBS_LOCK:
                j = dict(comic_api._JOBS.get(self.dir) or {})
            if not j.get("phase"):
                return j
            time.sleep(0.02)
        self.fail("补正后台任务超时未结束")

    def test_unknown_task_is_404(self):
        r = self.c.post("/api/comic/fix", json={"task": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_missing_brief_is_400(self):
        """老目录没 brief.txt，也不带 brief → 明确报错，别拿空需求去问模型。"""
        os.remove(os.path.join(self.dir, "brief.txt"))
        r = self.c.post("/api/comic/fix", json={"task": self.NAME})
        self.assertEqual(r.status_code, 400)
        self.assertIn("brief", r.get_json()["error"])

    def test_uses_original_draft_as_base(self):
        fake = {"text": self.FIXED, "issues": [], "diff": [], "adopted": False,
                "rejects": [], "warnings": [], "skipped": False, "model": ""}
        with mock.patch.object(comic_agent, "fix_script",
                               return_value=fake) as m:
            r = self.c.post("/api/comic/fix", json={"task": self.NAME})
            self.assertTrue(r.get_json()["ok"])
            self._idle()
        # 底稿必须是编剧原稿，不是已经补正过的当前稿
        self.assertEqual(m.call_args.args[1], self.DRAFT)

    def test_rewrites_script_and_writes_report(self):
        fake = {"text": self.FIXED, "issues": [{"no": "01",
                                                "kind": "missing_hair_color",
                                                "detail": "缺发色"}],
                "diff": [{"no": "01", "changed": ["scene"],
                          "before": {"scene": "1girl, twintails"},
                          "after": {"scene": "1girl, black hair, twintails"}}],
                "adopted": True, "rejects": [], "warnings": [],
                "skipped": False, "model": ""}
        with mock.patch.object(comic_agent, "fix_script", return_value=fake):
            self.c.post("/api/comic/fix", json={"task": self.NAME})
            self._idle()
        # 落盘时统一补一个结尾换行，比对前先 strip
        self.assertEqual(self._read("script.md").strip(), self.FIXED.strip())
        # fix.json 不能带全文（只放清单与 diff，页面每 2 秒读一次）
        import json
        report = json.loads(self._read("fix.json"))
        self.assertTrue(report["adopted"])
        self.assertNotIn("text", report)
        self.assertTrue(report["at"])

    def test_status_exposes_the_fix_report(self):
        import json
        self._write("fix.json", json.dumps({"adopted": True, "diff": []},
                                           ensure_ascii=False))
        d = self.c.get("/api/comic/status?task=" + self.NAME).get_json()
        self.assertTrue(d["fix"]["adopted"])

    def test_status_fix_is_null_for_old_tasks(self):
        d = self.c.get("/api/comic/status?task=" + self.NAME).get_json()
        self.assertIsNone(d["fix"])

    def test_script_text_returns_current_and_original(self):
        d = self.c.get("/api/comic/script_text?task=" + self.NAME).get_json()
        self.assertEqual(d["script"], self.FIXED)
        self.assertEqual(d["original"], self.DRAFT)

    def test_conflicts_with_a_running_job(self):
        with comic_api._JOBS_LOCK:
            comic_api._JOBS[self.dir] = {"phase": "bubble", "error": None}
        r = self.c.post("/api/comic/fix", json={"task": self.NAME})
        self.assertEqual(r.status_code, 409)


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

    def test_bubble_split_flag_is_passed_through(self):
        """`split` 决定这一格走一遍还是两遍——画崩了才切两遍。

        默认必须是一遍（2026-10-11 合并掉的第二次 i2i 就是靠这个默认值省下来的）。
        """
        with mock.patch.object(comic, "run") as m:
            r = self.c.post("/api/comic/bubble",
                            json={"task": self.NAME, "no": "01", "split": True})
            self.assertTrue(r.get_json()["split"])
            self._idle()
        self.assertTrue(m.call_args.kwargs.get("split_bubbles"))

    def test_bubble_split_defaults_to_one_pass(self):
        with mock.patch.object(comic, "run") as m:
            r = self.c.post("/api/comic/bubble",
                            json={"task": self.NAME, "no": "01"})
            self.assertFalse(r.get_json()["split"])
            self._idle()
        self.assertFalse(m.call_args.kwargs.get("split_bubbles"))

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
