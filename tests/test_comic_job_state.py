# -*- coding: utf-8 -*-
"""漫画后台任务的持久化与“重启后不静默消失”。

现场：ComfyUI 重启窗口里点“一键生成/一键加气泡”，半小时后回来发现
图没生成，而 state.json 还停在 pending/rendered，前端只显示“排队/待加
气泡”，看起来像有人会来跑，其实后台线程早没了。

这里钉住两件事：
1. `_spawn` 把“正在跑 / 跑完 / 挂了”写到任务目录的 job.json；
2. 5174 进程重启后第一次轮询能认出“上次是 running 但线程已经不在”，
   返回明确的中断原因和可续跑标记。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.comic import routes as comic_api
from app.features.comic import job_state


SCRIPT = "# 标题：测试\n\n## 01\n画面: 1girl, solo, pov\n气泡: 一\n标签1: a\n"


class SpawnJournalTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="comic_job_")
        self.path = os.path.join(self.tmp, "C20261118-99")
        os.makedirs(self.path)
        with open(os.path.join(self.path, "script.md"), "w", encoding="utf-8") as f:
            f.write(SCRIPT)
        state = {"task_id": "C20261118-99", "title": "测试", "script": "script.md",
                 "updated": "", "panels": {"01": {
                     "scene": "1girl, solo, pov", "bubble": "", "bubble2": "",
                     "label": "", "speaker": "女", "speaker2": "",
                     "state": "pending", "seed": None, "error": None,
                     "raw": "", "final": ""}}}
        with open(os.path.join(self.path, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn_with_fake_thread(self, fn, meta):
        started = {}
        with mock.patch.object(comic_api.threading, "Thread") as thread_cls:
            thread_cls.return_value.start.side_effect = (
                lambda: started.update(target=thread_cls.call_args.kwargs["target"]))
            comic_api._spawn(self.path, "scene", fn, meta=meta)
        return started["target"]

    def test_running_then_done_is_written(self):
        worker = self._spawn_with_fake_thread(lambda: None, {
            "kind": "scene", "resumable": True, "scene_skill": "anima-line"})

        # 线程还没体面退出前，磁盘上必须已经是 running（5174 随机会被重启）
        j = job_state.read(self.path)
        self.assertEqual("running", j["status"])
        self.assertEqual("scene", j["phase"])
        self.assertEqual("anima-line", (j.get("meta") or {}).get("scene_skill"))

        worker()
        j = job_state.read(self.path)
        self.assertEqual("done", j["status"])

    def test_error_is_written_before_memory_error(self):
        def boom():
            raise SystemExit("ComfyUI 没在线")

        worker = self._spawn_with_fake_thread(boom, {"kind": "scene"})
        worker()
        j = job_state.read(self.path)
        self.assertEqual("error", j["status"])
        self.assertIn("没在线", j["message"])
        # 内存里那条也保留，页面刷新前的轮询仍能看到原因
        with comic_api._JOBS_LOCK:
            self.assertIn("没在线", (comic_api._JOBS.get(self.path) or {}).get("error"))


class StaleJobStatusTest(unittest.TestCase):
    """进程重启后，job.json 说 running，但内存里没有线程。"""

    def setUp(self):
        self.c = comic_api.bp  # 仅确认蓝图存在；实际用 Flask test_client 更好
        self.tmp = tempfile.mkdtemp(prefix="comic_stale_")
        self.task = "C20261118-98"
        self.path = os.path.join(self.tmp, self.task)
        os.makedirs(self.path)
        with open(os.path.join(self.path, "script.md"), "w", encoding="utf-8") as f:
            f.write(SCRIPT)
        state = {"task_id": self.task, "title": "测试", "script": "script.md",
                 "updated": "", "panels": {"01": {
                     "scene": "1girl, solo, pov", "bubble": "", "bubble2": "",
                     "label": "", "speaker": "女", "speaker2": "",
                     "state": "pending", "seed": None, "error": None,
                     "raw": "", "final": ""}}}
        with open(os.path.join(self.path, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        self._patches = [
            mock.patch.object(comic_api, "_COMICS_ROOT", self.tmp),
            mock.patch.object(comic_api, "_comfy_state", lambda: True),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        with comic_api._JOBS_LOCK:
            comic_api._JOBS.pop(self.path, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_reports_interrupted_and_resumable(self):
        from app import main
        job_state.begin(self.path, "scene", {
            "kind": "scene", "resumable": True, "scene_skill": "anima-line"})
        # 模拟上一进程的 pid：当前进程里不可能有对应线程
        job_state.update(self.path, pid=1, started=time.time() - 3600)

        r = main.app.test_client().get(
            "/api/comic/status?task=" + self.task + "&include_test=1")
        self.assertEqual(200, r.status_code)
        d = r.get_json()
        self.assertIn("中断", d.get("job_error") or "")
        self.assertTrue((d.get("resume") or {}).get("can_resume"))
        self.assertEqual("scene", (d.get("resume") or {}).get("phase"))
        # 同时必须把“没人会来跑”的格标成出错，不能继续显示排队
        self.assertIn("中断", d["panels"][0]["error"] or "")
        self.assertNotEqual("running", job_state.read(self.path)["status"])
