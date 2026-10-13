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
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (comic, comic_agent, comic_api, comic_portrait,
                 comic_render_opts, main)

GOOD_SCRIPT = "# 标题：测试\n\n## 01\n画面: 1girl, solo, pov\n气泡: 一\n标签1: 她站在门口\n"


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
                    "status": "ok", "audit": [],
                    "warnings": [], "skipped": False, "model": ""}
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
        self.assertEqual(out["fix"]["status"], "error")
        self.assertTrue(any("补正异常" in x for x in out["fix"]["warnings"]))


class ComicFixApiTest(unittest.TestCase):
    """内容补正的 HTTP 面：重跑（/fix）与取正文（/script_text）。

    2026-10-11 十轮加。两条都是踩出来的：
      - /fix 的底稿必须是 **script_original.md**，不是当前 script.md——重跑要
        幂等：拿补正过的稿再补一次，会把上一轮的改动当成「原稿」来比对，diff
        越滚越乱。
      - /script_text 存在的唯一理由是堵一个数据事故：补正就地改 script.md，
        而页面 textarea 里还是旧文本，用户接着点「确认开跑」会把旧文本存回去，
        补正结果整个作废。

    ⚠️ 2026-10-12 用户拍板取消补正的准入闸门（代码判据只做检测），所以补正稿
    无论还剩几条问题都**照常落地**；fix.json 里也不再是 `rejects`，而是
    `status`（ok / empty / unparseable / structure / nochange / error）+ `audit`。
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
                "status": "nochange", "audit": [], "warnings": [],
                "skipped": False, "model": ""}
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
                "adopted": True, "status": "ok", "audit": [], "warnings": [],
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

    # 02 特意带 `气泡2`：/prompt 的 split 跟随要拿「有第二句台词」的格才看得出
    # 差别（没有气泡2 的格，两套模板拼出来是同一段）。
    SCRIPT = ("# 标题：t\n\n## 01\n画面: 1girl, solo\n气泡: 一\n标签1: 甲\n"
              "\n## 02\n画面: 1girl, solo\n气泡: 二\n气泡2: 二之二\n标签1: 乙\n")
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

    # ── /prompt（看清并手改发给 qwen 的文字）────────────

    def test_prompt_endpoint_returns_the_exact_text_sent_to_qwen(self):
        """取出来的必须**一字不差**就是 bubble_prompt() 的输出。

        用户 2026-10-11：「我需要看到完整的发给 qwen 的文字的内容是什么，我可以
        进行修改」。所以这条链上不许有任何二次包装——一旦这里加工过，界面上看到
        的是 A、真正跑出去的是 B，用户改了半天改的是个影子。
        """
        d = self.c.get("/api/comic/prompt?task=%s&no=1" % self.NAME).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["no"], "01")             # no 补零成两位
        panel = next(p for p in self._load().panels if p.no == "01")
        self.assertEqual(d["prompt"], panel.bubble_prompt(split=False))

    def test_prompt_endpoint_follows_the_split_switch(self):
        """split 跟着界面上的画法下拉走。

        一格有没有第二句台词，拼出来的是**两套模板**；不跟随的话用户看着一遍画完
        的文字、实际按分开画跑，改的字落不到该落的地方。
        """
        a = self.c.get("/api/comic/prompt?task=%s&no=02&split=0" % self.NAME).get_json()
        b = self.c.get("/api/comic/prompt?task=%s&no=02&split=1" % self.NAME).get_json()
        self.assertFalse(a["split"])
        self.assertTrue(b["split"])
        self.assertNotEqual(a["prompt"], b["prompt"])

    def test_prompt_unknown_panel_is_404(self):
        r = self.c.get("/api/comic/prompt?task=%s&no=09" % self.NAME)
        self.assertEqual(r.status_code, 404)

    def test_bubble_prompt_override_goes_straight_to_render_bubble(self):
        """手改的提示词走**单格直跑**，且原样交给 render_bubble。

        不能走 comic.run——那是「按剧本逐格跑」的批处理，提示词由它自己按模板拼，
        没有「这一格换一段文字」的入口。
        """
        mine = '在画面左上角添加一个对话框，里面写 "我改过的"。'
        with mock.patch.object(comic, "render_bubble") as m, \
                mock.patch.object(comic, "run") as run:
            d = self.c.post("/api/comic/bubble",
                            json={"task": self.NAME, "no": "01",
                                  "prompt": mine}).get_json()
            self.assertTrue(d["ok"])
            self.assertTrue(d.get("override"))
            self._idle()
        self.assertFalse(run.called, "手改提示词不该走 comic.run")
        self.assertEqual(m.call_args.kwargs.get("prompt"), mine)

    def test_bubble_prompt_override_keeps_the_split_flag(self):
        # 用 01（setUp 里置成 done）——02 还是 pending，没有场景图，压根到不了
        # render_bubble（那条 400 由 test_bubble_still_rejects_pending_panel 管）。
        with mock.patch.object(comic, "render_bubble") as m:
            self.c.post("/api/comic/bubble",
                        json={"task": self.NAME, "no": "01", "prompt": "改",
                              "split": True})
            self._idle()
        self.assertTrue(m.call_args.kwargs.get("split"))

    def test_bubble_rejects_override_for_all_panels(self):
        """no=all 时把同一段手改文字套到每一格上是灾难，直接拒。

        「全部」是批处理，每一格的台词都不一样——用户改的那段只对某一格有意义。
        """
        r = self.c.post("/api/comic/bubble",
                        json={"task": self.NAME, "no": "all", "prompt": "改"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("单格", r.get_json()["error"])

    def test_bubble_prompt_override_conflicts_with_a_running_job(self):
        with comic_api._JOBS_LOCK:
            comic_api._JOBS[self.dir] = {"phase": "bubble", "error": None}
        r = self.c.post("/api/comic/bubble",
                        json={"task": self.NAME, "no": "01", "prompt": "改"})
        self.assertEqual(r.status_code, 409)

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


class ComicRenderOptsApiTest(unittest.TestCase):
    """渲染参数覆盖 API（2026-10-13）：GET 回显模板默认值 / POST 存 render.json /
    reset 删掉 / 越界 400。资源清单 mock 成固定值——不依赖 ComfyUI 在不在线
    （在线时名单是真的，这里要测的是「名单里的收、名单外的拒」这条判据）。"""

    NAME = "C20261013-01"

    def setUp(self):
        self.c = main.app.test_client()
        self.tmp = tempfile.mkdtemp(prefix="comic_render_api_")
        self.patch_root = mock.patch.object(comic_api, "_COMICS_ROOT", self.tmp)
        self.patch_root.start()
        # 清单必须盖住模板自带的 6 个 LoRA + 测试用的假角色名——不然默认值里
        # 的 star cat / cowani / reina 会被「名单校验」挡下，测的就跑偏了。
        names = [s["name"] for s in
                 comic_render_opts.defaults("anima-line")["loras"]]
        self.res = {"loras": sorted(set(names + ["char_x.safetensors"])),
                    "samplers": ["er_sde", "euler", "dpmpp_2m"],
                    "schedulers": ["simple", "karras"],
                    "upscales": ["2x_Ani4Kv2_G6i2_Compact_107500.pth",
                                 "4x_NMKD-Siax_200k.pth"],
                    "online": True}
        self.patch_res = mock.patch.object(comic_api, "_render_resources",
                                           return_value=self.res)
        self.patch_res.start()
        self.dir = os.path.join(self.tmp, self.NAME)
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "script.md"), "w", encoding="utf-8") as f:
            f.write("# 标题：t\n\n## 01\n画面: 1girl, solo\n气泡: 一\n标签1: 甲\n")

    def tearDown(self):
        self.patch_res.stop()
        self.patch_root.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _opts(self, **kw):
        o = comic_render_opts.defaults("anima-line")
        o.update(kw)
        return o

    def test_get_returns_template_defaults_when_never_saved(self):
        r = self.c.get("/api/comic/render_opts?task=" + self.NAME)
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["saved"])
        self.assertEqual(len(d["opts"]["loras"]), 6)
        self.assertEqual(d["opts"]["base"]["sampler_name"], "er_sde")
        self.assertEqual(d["resources"]["loras"], self.res["loras"])

    def test_get_unknown_task_is_404(self):
        r = self.c.get("/api/comic/render_opts?task=C99990101-99")
        self.assertEqual(r.status_code, 404)

    def test_save_then_get_reflects_it(self):
        opts = self._opts()
        opts["loras"][0] = {"name": "char_x.safetensors", "strength": 0.75}
        opts["base"]["steps"] = 24
        r = self.c.post("/api/comic/render_opts",
                        json={"task": self.NAME, "opts": opts})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        d = self.c.get("/api/comic/render_opts?task=" + self.NAME).get_json()
        self.assertTrue(d["saved"])
        self.assertEqual(d["opts"]["loras"][0]["name"], "char_x.safetensors")
        self.assertEqual(d["opts"]["base"]["steps"], 24)

    def test_lora_outside_the_list_is_400(self):
        opts = self._opts()
        opts["loras"][0] = {"name": "no_such.safetensors", "strength": 0.8}
        r = self.c.post("/api/comic/render_opts",
                        json={"task": self.NAME, "opts": opts})
        self.assertEqual(r.status_code, 400)
        self.assertIn("没有 LoRA", r.get_json()["error"])
        self.assertFalse(os.path.isfile(
            os.path.join(self.dir, "render.json")))

    def test_bad_width_is_400_and_nothing_written(self):
        opts = self._opts(width=1001)
        r = self.c.post("/api/comic/render_opts",
                        json={"task": self.NAME, "opts": opts})
        self.assertEqual(r.status_code, 400)
        self.assertIn("8 的倍数", r.get_json()["error"])
        self.assertFalse(os.path.isfile(
            os.path.join(self.dir, "render.json")))

    def test_clamped_values_come_back_with_warnings(self):
        opts = self._opts()
        opts["base"]["steps"] = 999
        r = self.c.post("/api/comic/render_opts",
                        json={"task": self.NAME, "opts": opts})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["warnings"])
        self.assertEqual(r.get_json()["opts"]["base"]["steps"], 150)

    def test_reset_deletes_render_json(self):
        self.c.post("/api/comic/render_opts",
                    json={"task": self.NAME, "opts": self._opts(base={"steps": 24})})
        r = self.c.post("/api/comic/render_opts",
                        json={"task": self.NAME, "reset": True})
        self.assertTrue(r.get_json()["reset"])
        self.assertFalse(os.path.isfile(
            os.path.join(self.dir, "render.json")))
        d = self.c.get("/api/comic/render_opts?task=" + self.NAME).get_json()
        self.assertFalse(d["saved"])
        self.assertEqual(d["opts"]["base"]["steps"], 20)   # 回模板默认值

    def test_upscale_save_roundtrip_and_unknown_model_400(self):
        opts = self._opts()
        opts["upscale"] = {"scale_by": 1.5,
                           "final_model": "4x_NMKD-Siax_200k.pth",
                           "final_skip": False}
        r = self.c.post("/api/comic/render_opts",
                        json={"task": self.NAME, "opts": opts})
        self.assertEqual(r.status_code, 200)
        d = self.c.get("/api/comic/render_opts?task=" + self.NAME).get_json()
        self.assertEqual(d["opts"]["upscale"]["scale_by"], 1.5)
        self.assertEqual(d["opts"]["upscale"]["final_model"],
                         "4x_NMKD-Siax_200k.pth")
        # 名单外的放大模型 → 400（事实问题，拒）
        opts["upscale"]["final_model"] = "nope.pth"
        r = self.c.post("/api/comic/render_opts",
                        json={"task": self.NAME, "opts": opts})
        self.assertEqual(r.status_code, 400)
        self.assertIn("放大模型", r.get_json()["error"])


class ComicPortraitApiTest(unittest.TestCase):
    """生图模式 API（2026-10-13）：建任务跑批次 / 轮询 / 单张换种子 / 图服务。

    comic_portrait.run 被 mock 成同步假跑——这里测的是路由层的行为（任务目录
    怎么建、409 互斥、账本怎么回给前端），不真连 ComfyUI。
    """

    NAME = "C20261013-02"

    def setUp(self):
        self.c = main.app.test_client()
        self.tmp = tempfile.mkdtemp(prefix="comic_portrait_api_")
        self.patch_root = mock.patch.object(comic_api, "_COMICS_ROOT", self.tmp)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.dir = os.path.join(self.tmp, self.NAME)
        os.makedirs(self.dir, exist_ok=True)

    def _fake_run(self, task_dir, brief, heroine, n, **kw):
        """假跑一批：账本 + 图落盘，和真的 run 一个形状。"""
        comic_portrait.save_meta(task_dir, brief, heroine, n)
        shots = []
        os.makedirs(comic_portrait.shots_dir(task_dir), exist_ok=True)
        for i in range(1, n + 1):
            no = "%02d" % i
            with open(comic_portrait.shot_path(task_dir, no), "wb") as f:
                f.write(b"png")
            shots.append({"no": no, "prompt": "1girl, scene %s" % no,
                          "seed": 1000 + i, "state": "rendered", "error": None})
        comic_portrait.save_prompts(task_dir, shots)
        return shots

    def test_start_creates_task_when_none_given(self):
        with mock.patch.object(comic_portrait, "run", side_effect=self._fake_run):
            r = self.c.post("/api/comic/portrait",
                            json={"brief": "花田写真", "n": 3})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertTrue(d["task"].startswith("C"))
        # 后台线程跑完再断言（轮询到 done）
        s = self._wait_done(d["job"])
        # 新目录真的建出来了，账本在里面
        path = os.path.join(self.tmp, d["task"])
        self.assertTrue(os.path.isfile(comic_portrait.prompts_path(path)))
        self.assertEqual(len(comic_portrait.load_prompts(path)), 3)
        # 轮询拿到 done + shots（带 URL 和指纹）
        self.assertEqual(s["state"], "done")
        self.assertEqual(len(s["shots"]), 3)
        self.assertIn("f=portraits/01.png", s["shots"][0]["url"])

    def _wait_done(self, job, tries=50):
        """轮询到批次结束（后台线程是异步的，不能 POST 完就断言）。"""
        for _ in range(tries):
            s = self.c.get("/api/comic/portrait_status?job=" + job).get_json()
            if s.get("state") != "running":
                return s
            time.sleep(0.1)
        self.fail("写真批次迟迟不结束")

    def test_existing_task_is_reused(self):
        with mock.patch.object(comic_portrait, "run", side_effect=self._fake_run):
            r = self.c.post("/api/comic/portrait",
                            json={"task": self.NAME, "brief": "天台", "n": 2})
        self.assertEqual(r.get_json()["task"], self.NAME)

    def test_missing_brief_is_400(self):
        r = self.c.post("/api/comic/portrait", json={"n": 3})
        self.assertEqual(r.status_code, 400)

    def test_second_batch_while_running_is_409(self):
        gate = threading.Event()

        def _slow_run(task_dir, brief, heroine, n, **kw):
            gate.wait(5)
            return self._fake_run(task_dir, brief, heroine, n, **kw)

        with mock.patch.object(comic_portrait, "run", side_effect=_slow_run):
            self.c.post("/api/comic/portrait",
                        json={"task": self.NAME, "brief": "a", "n": 2})
            r = self.c.post("/api/comic/portrait",
                            json={"task": self.NAME, "brief": "b", "n": 2})
            self.assertEqual(r.status_code, 409)
            gate.set()

    def test_shot_reseed_updates_ledger(self):
        comic_portrait.save_prompts(self.dir, [
            {"no": "01", "prompt": "1girl, wheat field", "seed": 111,
             "state": "error", "error": "上次炸了"}])
        with mock.patch.object(comic_portrait, "reseed_shot",
                               return_value={"no": "01", "prompt": "1girl, wheat field",
                                             "seed": 999, "state": "rendered",
                                             "error": None}):
            r = self.c.post("/api/comic/portrait_shot",
                            json={"task": self.NAME, "no": "1"})
        self.assertEqual(r.status_code, 200)
        shot = r.get_json()["shot"]
        self.assertEqual(shot["seed"], 999)
        self.assertEqual(shot["no"], "01")

    def test_shot_reseed_unknown_no_is_400(self):
        comic_portrait.save_prompts(self.dir, [
            {"no": "01", "prompt": "x", "seed": 1,
             "state": "rendered", "error": None}])
        r = self.c.post("/api/comic/portrait_shot",
                        json={"task": self.NAME, "no": "07"})
        self.assertEqual(r.status_code, 400)

    def test_img_serves_portraits_and_rejects_junk(self):
        os.makedirs(comic_portrait.shots_dir(self.dir), exist_ok=True)
        with open(comic_portrait.shot_path(self.dir, "01"), "wb") as f:
            f.write(b"png")
        ok = self.c.get("/api/comic/img?task=%s&f=portraits/01.png" % self.NAME)
        self.assertEqual(ok.status_code, 200)
        bad = self.c.get("/api/comic/img?task=%s&f=portraits/1.png" % self.NAME)
        self.assertEqual(bad.status_code, 404)
        evil = self.c.get("/api/comic/img?task=%s&f=../script.md" % self.NAME)
        self.assertEqual(evil.status_code, 404)

    def test_status_tolerates_pure_portrait_task(self):
        """没有 script.md 的纯写真任务：status 不 404，panels 空 + portraits 照返。"""
        comic_portrait.save_prompts(self.dir, [
            {"no": "01", "prompt": "1girl, wheat field", "seed": 111,
             "state": "rendered", "error": None}])
        os.makedirs(comic_portrait.shots_dir(self.dir), exist_ok=True)
        with open(comic_portrait.shot_path(self.dir, "01"), "wb") as f:
            f.write(b"png")
        r = self.c.get("/api/comic/status?task=" + self.NAME)
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["panels"], [])
        self.assertEqual(len(d["portraits"]), 1)


if __name__ == "__main__":
    unittest.main()
