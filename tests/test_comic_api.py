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


if __name__ == "__main__":
    unittest.main()
