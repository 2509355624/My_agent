# -*- coding: utf-8 -*-
"""单张 / 单格「终止」+ 真探活（2026-11-23）。

用户当天报的三件事，正好是这里的三组用例：

1. **终止了还在画**：跑图台 / moss 漫画台的「终止」原来只置位本台那份
   Event，`image_jobs.process` 提交 ComfyUI 之前根本不看它，于是图照样
   画完、照样投递。→ `cancel_tag` 标 `stopped`，process 在提交前 / 投递前
   各看一眼。
2. **单个格子也要能单独停**：不能让「停一格」变成「全停」。→
   `cancel_tag` 只认一个 tag；moss 的 `_PAGE_STOP` 只跳过被点名的那一页。
3. **重启完 ComfyUI，网页还是画图中**：原来提交完就只等回执，从不问
   「它到底在不在队列里」。→ `/api/gen/status` 带 `phase`，
   `/api/moss/pages_state` 按页回 `phase`，问不到就说 lost。
"""

import os
import threading
import time
import unittest
from unittest import mock

from app.features.image import jobs as image_jobs


# ── 1. 队列里单个任务的摘除 / 标记 ───────────────────────────
class CancelTagTest(unittest.TestCase):
    def setUp(self):
        image_jobs._reset()

    def tearDown(self):
        image_jobs._reset()

    def _job(self, tag):
        job = image_jobs.Job(None, None, "{}", skill="silver", tag=tag)
        job.chan = image_jobs._COMFY
        return job

    def test_queued_job_is_removed_from_queue_and_slot_refunded(self):
        j = self._job("GP-1")
        image_jobs._COMFY.queue.append(j)
        image_jobs._COMFY.per_session[("", "")] = 1

        self.assertTrue(image_jobs.cancel_tag("GP-1"))

        self.assertEqual(0, len(image_jobs._COMFY.queue))
        self.assertNotIn(("", ""), image_jobs._COMFY.per_session)
        self.assertTrue(j.done.is_set())
        self.assertTrue(j.stopped)

    def test_running_job_is_marked_stopped_but_not_removed(self):
        j = self._job("GP-2")
        image_jobs._COMFY.running = [j]
        image_jobs._COMFY.per_session[("", "")] = 1

        self.assertTrue(image_jobs.cancel_tag("GP-2"))

        # 正在跑的不能从 running 里硬摘——worker 收尾要靠它
        self.assertEqual([j], image_jobs._COMFY.running)
        self.assertTrue(j.cancel.is_set())
        self.assertTrue(j.stopped)
        self.assertEqual(1, image_jobs._COMFY.per_session[("", "")])

    def test_only_the_named_one_is_touched(self):
        a, b = self._job("GP-a"), self._job("GP-b")
        image_jobs._COMFY.queue.extend([a, b])

        image_jobs.cancel_tag("GP-a")

        self.assertEqual([b], list(image_jobs._COMFY.queue))
        self.assertFalse(b.stopped)
        self.assertFalse(b.cancel.is_set())

    def test_unknown_tag_returns_false(self):
        self.assertFalse(image_jobs.cancel_tag("GP-none"))

    def test_queue_view_reports_stage(self):
        a, b = self._job("GP-a"), self._job("GP-b")
        image_jobs._COMFY.queue.append(a)
        image_jobs._COMFY.running = [b]
        self.assertEqual("queued", image_jobs.queue_view("GP-a"))
        self.assertEqual("running", image_jobs.queue_view("GP-b"))
        self.assertEqual("", image_jobs.queue_view("GP-x"))


# ── 2. process 在提交前 / 投递前看 stopped ────────────────────
class ProcessHonoursStopTest(unittest.TestCase):
    def setUp(self):
        image_jobs._reset()

    def tearDown(self):
        image_jobs._reset()

    def _job(self, stopped):
        job = image_jobs.Job(None, None, {"1": {}}, skill="silver", tag="GP-s")
        job.stopped = stopped
        return job

    def test_stopped_job_never_reaches_comfyui(self):
        job = self._job(True)
        with mock.patch.object(image_jobs, "_queue_prompt_retry") as submit, \
                mock.patch.object(image_jobs, "_maybe_restart_for_clean_start"), \
                mock.patch.object(image_jobs, "_maybe_restart_for_heavy_switch"), \
                mock.patch.object(image_jobs, "_maybe_release_for_switch"), \
                mock.patch.object(image_jobs, "_maybe_release_for_low_vram"):
            image_jobs.process(job)
        submit.assert_not_called()
        self.assertIsInstance(job.error, image_jobs.Cancelled)
        self.assertFalse(job.skill_done)

    def test_stop_arriving_during_setup_blocks_submit(self):
        """装模型那几十秒里点了终止：进程内标记一变，提交前就该收手。"""
        job = self._job(False)

        def _switch(_job):
            _job.stopped = True          # 用户在这期间点了终止

        with mock.patch.object(image_jobs, "_queue_prompt_retry") as submit, \
                mock.patch.object(image_jobs, "_maybe_restart_for_clean_start", _switch), \
                mock.patch.object(image_jobs, "_maybe_restart_for_heavy_switch"), \
                mock.patch.object(image_jobs, "_maybe_release_for_switch"), \
                mock.patch.object(image_jobs, "_maybe_release_for_low_vram"):
            image_jobs.process(job)
        submit.assert_not_called()
        self.assertIsInstance(job.error, image_jobs.Cancelled)


# ── 3. worker 收尾：停了就真去 ComfyUI 里掐 + 等退场 ──────────
class WorkerKillsStoppedJobTest(unittest.TestCase):
    def setUp(self):
        image_jobs._reset()

    def tearDown(self):
        image_jobs._reset()

    def test_worker_aborts_prompt_and_waits_idle(self):
        job = image_jobs.Job(None, None, {}, skill="silver", tag="GP-w")
        job.prompt_id = "pid-1"

        def _process(j):
            # 跑到采样中途被终止：process 自己返回（worker 那边不会再抛）
            j.stopped = True
            j.error = image_jobs.Cancelled("用户终止了这个任务")

        # 有界：断言完就把 worker 卡在一个永不返回的 _take 上，再把它的异常
        # 吞掉——真线程跑到 StopIteration 会在日志里刷一屏 traceback。
        def _take(_chan):
            if _take.n == 0:
                _take.n = 1
                return job
            time.sleep(30)
            return None

        _take.n = 0

        with mock.patch.object(image_jobs, "_take", _take), \
                mock.patch.object(image_jobs, "_abort") as abort, \
                mock.patch.object(image_jobs, "_wait_comfy_idle") as idle, \
                mock.patch.object(image_jobs, "process", _process), \
                mock.patch.object(image_jobs, "_maybe_restart_for_ram"):
            image_jobs._ensure_worker = lambda: None
            t = threading.Thread(target=image_jobs._worker,
                                 args=(image_jobs._COMFY,), daemon=True)
            t.start()
            for _ in range(100):
                if abort.called:
                    break
                time.sleep(0.02)
        abort.assert_called_with("pid-1")
        idle.assert_called()


# ── 4. 跑图台：终止接口 + status 的 phase ────────────────────
class GenStatusPhaseTest(unittest.TestCase):
    def setUp(self):
        from app import main
        from app.features.image import routes as gen_api
        self.gen_api = gen_api
        self.c = main.app.test_client()
        gen_api._JOBS.clear()

    def tearDown(self):
        self.gen_api._JOBS.clear()

    def _put(self, state="queued", **kw):
        jid = "GP-TEST-%d" % len(self.gen_api._JOBS)
        job = {"id": jid, "skill": "silver", "prompt": "x", "seed": 1,
               "state": state, "error": "", "file": "", "ts": time.time(),
               "loras": [], "abort": threading.Event()}
        job.update(kw)
        with self.gen_api._JOBS_LOCK:
            self.gen_api._JOBS[jid] = job
        return jid

    def _phase(self, jid):
        d = self.c.get("/api/gen/status").get_json()
        return next(j for j in d["jobs"] if j["id"] == jid)

    def test_queued_phase_when_still_in_our_queue(self):
        jid = self._put()
        with mock.patch.object(image_jobs, "snapshot",
                               return_value={"running": None,
                                             "queued": [{"tag": jid, "ahead": 2}]}):
            self.assertEqual("queued", self._phase(jid)["phase"])

    def test_running_phase_when_comfy_knows_the_prompt(self):
        jid = self._put(pid="pid-x")
        # running 里放一个**别的** tag：否则会先命中「就是我 = submit」那条
        with mock.patch.object(image_jobs, "snapshot",
                               return_value={"running": {"tag": "GP-other",
                                                         "pid": "pid-y"},
                                             "queued": []}), \
                mock.patch.object(image_jobs, "_comfy_up", return_value=True), \
                mock.patch.object(image_jobs, "_comfy_queue_has", return_value=True):
            self.assertEqual("running", self._phase(jid)["phase"])

    def test_lost_phase_when_comfy_restarted_and_forgot_it(self):
        """用户报的那一幕：ComfyUI 重启过，两边都没有这一张。"""
        jid = self._put(pid="pid-gone")
        with mock.patch.object(image_jobs, "snapshot",
                               return_value={"running": {"tag": "GP-other",
                                                         "pid": "pid-y"},
                                             "queued": []}), \
                mock.patch.object(image_jobs, "_comfy_up", return_value=True), \
                mock.patch.object(image_jobs, "_comfy_queue_has", return_value=False), \
                mock.patch.object(image_jobs, "poll_once", return_value=None):
            self.assertEqual("lost", self._phase(jid)["phase"])

    def test_lost_phase_when_comfy_is_down(self):
        jid = self._put(pid="pid-x")
        with mock.patch.object(image_jobs, "snapshot",
                               return_value={"running": {"tag": "GP-other",
                                                         "pid": "pid-y"},
                                             "queued": []}), \
                mock.patch.object(image_jobs, "_comfy_up", return_value=False):
            self.assertEqual("lost", self._phase(jid)["phase"])

    def test_cancel_marks_job_and_queue(self):
        jid = self._put()
        with mock.patch.object(image_jobs, "cancel_tag", return_value=True) as ct:
            d = self.c.post("/api/gen/cancel", json={"id": jid}).get_json()
        self.assertTrue(d["ok"])
        ct.assert_called_once()
        self.assertEqual(jid, ct.call_args[0][0])
        self.assertEqual("cancelled", self.gen_api._JOBS[jid]["state"])

    def test_cancel_of_finished_job_is_rejected(self):
        jid = self._put(state="done")
        d = self.c.post("/api/gen/cancel", json={"id": jid}).get_json()
        self.assertFalse(d["ok"])


# ── 5. moss 漫画台：单页终止只跳这一页 ───────────────────────
class MossPageStopTest(unittest.TestCase):
    def test_page_stop_set_only_skips_named_page(self):
        from app.features.comic import moss_api
        with moss_api._RUN_LOCK:
            moss_api._PAGE_STOP["/tmp/task"] = {2}
        self.assertTrue(moss_api._page_stopped("/tmp/task", 2))
        self.assertFalse(moss_api._page_stopped("/tmp/task", 3))
        self.assertFalse(moss_api._page_stopped("/tmp/other", 2))
        with moss_api._RUN_LOCK:
            moss_api._PAGE_STOP.pop("/tmp/task", None)


if __name__ == "__main__":
    unittest.main()
