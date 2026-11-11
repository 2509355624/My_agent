# -*- coding: utf-8 -*-
"""漫画台「独占通道」测试（2026-11-11）。

钉死五件事：
  1. `comfy_gate` 优先票：文件名 `!` 前缀在字典序上压后所有普通票，多张
     优先票之间仍按各自时间戳 FIFO（机制没动，只是排序占先）。
  2. `image_jobs.enqueue(priority=True)`：绕过每会话在途上限（独占的本意
     就是把一整批最多 10 张一次推进队列），全局 max_queue 仍然生效。
  3. `comic._submit(priority=..., wait=False)`：入队即返回 job 句柄，
     `_collect` 收图；`_submit` 把 priority 透传给 enqueue。
  4. `comic.run(exclusive=True)`：阶段一把所有待跑格**一次性入队**（NAI
     渠道除外——云端队列管不着），前 EXCLUSIVE_MAX 张优先票、之后普通票；
     收图后每格照常落状态、记耗时；逐格路径（exclusive=False）行为不变。
  5. `/api/comic/start` 只认站主：直连（无 qq）传 exclusive 生效；群友经
     5175 反代必带 qq，传了也当没传。群友页由派生脚本隐藏这个勾选框。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_api, comfy_gate, image_jobs, main

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _script(n):
    """n 格剧本（每格一个画面行，别的不需要）。"""
    parts = ["# 标题：独占测试\n"]
    for i in range(1, n + 1):
        parts.append("\n## %02d\n画面: 1girl, solo, room\n" % i)
    return "".join(parts)


def _mk_script(tmp, n):
    path = os.path.join(tmp, "script.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_script(n))
    return path


class GatePriorityTicketTest(unittest.TestCase):
    """优先票的排序：! 前缀压后所有普通票。"""

    def test_priority_prefix_sorts_before_normal(self):
        a = os.path.basename(comfy_gate._ticket_path(True))
        b = os.path.basename(comfy_gate._ticket_path(False))
        self.assertTrue(a.startswith("!"), "优先票文件名要有 ! 前缀")
        self.assertFalse(b.startswith("!"))
        self.assertLess(a, b, "sorted() 一比优先票就该排前面")

    def test_priority_tickets_fifo_among_themselves(self):
        # 优先票之间没有特权：早登记的在前（时间戳定长，字典序即先后）。
        import time as _time
        first = os.path.basename(comfy_gate._ticket_path(True))
        _time.sleep(0.000001)                       # 保证 time_ns 不重复
        second = os.path.basename(comfy_gate._ticket_path(True))
        self.assertLess(first, second)

    def test_make_ticket_priority_lands_in_queue_dir(self):
        fp = comfy_gate.make_ticket(priority=True)
        self.addCleanup(comfy_gate._drop_ticket, fp)
        self.assertTrue(fp)
        self.assertTrue(os.path.basename(fp).startswith("!"))
        self.assertIn(fp, [os.path.join(comfy_gate.queue_dir(), n)
                           for n in comfy_gate._tickets()])


class EnqueuePriorityTest(unittest.TestCase):
    """priority=True 绕过每会话在途上限（全局 max_queue 仍然生效）。"""

    def setUp(self):
        image_jobs._reset()
        # worker 线程挡在门外（_Base 的规矩）：任务入队后由测试自己驱动。
        p = mock.patch.object(image_jobs, "_ensure_worker", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(image_jobs._reset)

    def test_priority_bypasses_per_session_cap(self):
        ctx = ("group", "42")
        for _ in range(image_jobs.MAX_INFLIGHT):
            job, reason = image_jobs.enqueue(ctx[0], ctx[1], {"1": {}}, None)
            self.assertIsNone(reason)
        over, reason = image_jobs.enqueue(ctx[0], ctx[1], {"1": {}}, None)
        self.assertIsNone(over)
        self.assertIn("排着", reason)
        prio, reason = image_jobs.enqueue(ctx[0], ctx[1], {"1": {}}, None,
                                          priority=True)
        self.assertIsNone(reason, "优先票不该被每会话上限拒掉")
        self.assertIsNotNone(prio)
        self.assertTrue(prio.priority)

    def test_normal_job_defaults_to_not_priority(self):
        job, reason = image_jobs.enqueue("group", "7", {"1": {}}, None)
        self.assertIsNone(reason)
        self.assertFalse(job.priority, "不传 priority 的老路径必须是 False")

    def test_priority_still_respects_global_queue(self):
        # 全局闸对优先票照样生效——这是真正防失控的那道。
        for i in range(image_jobs.MAX_QUEUE):
            image_jobs.enqueue("group", str(i), {"1": {}}, None)
        prio, reason = image_jobs.enqueue("group", "x", {"1": {}}, None,
                                          priority=True)
        self.assertIsNone(prio)
        self.assertIn("排队的人太多", reason)


class _FakeJob(object):
    def __init__(self, entry):
        self.entry = entry
        self.priority = False
        self.cancel = None

    def wait(self):
        return self.entry


class SubmitWaitFalseTest(unittest.TestCase):
    """_submit(wait=False) 入队即返回句柄；_collect 收图。"""

    def test_submit_nowait_returns_job_and_passes_priority(self):
        fake = _FakeJob({"images": ["a.png"]})
        captured = {}

        def fake_enqueue(target, tid, wf, skill=None, **kw):
            captured.update(kw)
            return fake, None

        with mock.patch.object(comic.image_jobs, "enqueue", fake_enqueue), \
                mock.patch.object(comic.image_jobs, "output_images",
                                  lambda entry, job=None: ["a.png"]):
            job = comic._submit({"1": {}}, "anima-line", "p", 1,
                                priority=True, wait=False)
            self.assertIs(job, fake)
            self.assertTrue(captured.get("priority"))
            # wait=True（默认）仍然走完整路径、返回文件名
            name = comic._submit({"1": {}}, "anima-line", "p", 1,
                                 priority=False)
            self.assertEqual(name, "a.png")
            self.assertFalse(captured.get("priority"))


class RunExclusiveTest(unittest.TestCase):
    """run(exclusive=True)：一次性入队、前 10 张优先票、收图照常落状态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, n, **kw):
        script = _mk_script(self.tmp.name, n)
        submitted = []                       # (priority, wait) 按入队顺序

        def fake_submit(wf, skill, prompt, seed, task=None, out_dir=None,
                        abort=None, with_entry=False, priority=False,
                        wait=True):
            submitted.append((priority, wait))
            return _FakeJob({"images": ["x.png"]})

        def fake_collect(job, with_entry=False):
            if with_entry:
                return "x.png", {"images": ["x.png"]}
            return "x.png"

        with mock.patch.object(comic, "_require_comfy", lambda: None), \
                mock.patch.object(comic, "_scene_wf",
                                  lambda task, p, skill, opts=None: (99, {})), \
                mock.patch.object(comic, "_submit", side_effect=fake_submit), \
                mock.patch.object(comic, "_collect", side_effect=fake_collect), \
                mock.patch.object(comic, "_download", lambda name, path: None):
            task = comic.run(script, silver_only=True, scene_skill="anima-line",
                             **kw)
        return task, submitted

    def test_all_pending_submitted_upfront_as_priority(self):
        task, submitted = self._run(3, exclusive=True)
        self.assertEqual(len(submitted), 3)
        self.assertTrue(all(p and not w for p, w in submitted),
                        "独占 = 全部 wait=False 入队，前 10 张全是优先票")
        for p in task.panels:
            self.assertEqual(p.state, "rendered")
            self.assertEqual(p.seed, 99)
            self.assertIsNotNone(p.scene_wall, "收图后 wall 照常记")

    def test_over_max_later_panels_are_normal_tickets(self):
        task, submitted = self._run(12, exclusive=True)
        self.assertEqual(len(submitted), 12)
        flags = [p for p, _ in submitted]
        self.assertEqual(flags[:comic.EXCLUSIVE_MAX], [True] * comic.EXCLUSIVE_MAX)
        self.assertEqual(flags[comic.EXCLUSIVE_MAX:], [False, False],
                         "超出限额的格照样提前入队，但登记普通票")

    def test_not_exclusive_uses_sequential_path(self):
        # exclusive=False：render_scene 逐格路径，行为与从前完全一致。
        script = _mk_script(self.tmp.name, 2)
        rs = mock.Mock(side_effect=lambda task, p, skill, opts=None,
                       abort=None: setattr(p, "state", "rendered"))
        with mock.patch.object(comic, "_require_comfy", lambda: None), \
                mock.patch.object(comic, "render_scene", rs):
            task = comic.run(script, silver_only=True, scene_skill="anima-line")
        self.assertEqual(rs.call_count, 2)
        for p in task.panels:
            self.assertEqual(p.state, "rendered")

    def test_exclusive_nai_falls_back_to_sequential(self):
        # NAI 云端管不着独木桥顺序：exclusive 对它不生效，退回逐格跑。
        script = _mk_script(self.tmp.name, 2)
        rs = mock.Mock(side_effect=lambda task, p, skill, opts=None,
                       abort=None: setattr(p, "state", "rendered"))
        with mock.patch.object(comic, "_require_comfy", lambda: None), \
                mock.patch.object(comic, "render_scene", rs), \
                mock.patch.object(comic, "_run_scene_exclusive") as batch:
            comic.run(script, silver_only=True, scene_skill="nai",
                      exclusive=True)
        self.assertFalse(batch.called, "NAI 渠道不该走独占批量")
        self.assertEqual(rs.call_count, 2)


class ApiStartExclusiveTest(unittest.TestCase):
    """「独占通道」只认站主：直连（无 qq）生效，群友传了也当没传。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = main.app.test_client()

    def _start(self, qq):
        path = _mk_script(self.tmp.name, 2)
        meta, panels = comic.parse_script(path)
        task = comic.Task(path, meta, panels)
        spawned = {}
        run_calls = []

        def fake_spawn(path_, phase, fn, on_error=None, finish=None):
            spawned["fn"] = fn
            return True

        def fake_run(script_path, **kw):
            run_calls.append(kw)
            return task

        with mock.patch.object(comic_api, "_task_dir", lambda t: self.tmp.name), \
                mock.patch.object(comic_api, "_load_task", lambda p: task), \
                mock.patch.object(comic_api, "_qq", lambda: qq), \
                mock.patch.object(comic_api, "_resolve_skill",
                                  lambda p, s: "anima-line"), \
                mock.patch.object(comic_api, "_save_task_skill",
                                  lambda p, s: None), \
                mock.patch.object(comic_api, "_spawn", fake_spawn), \
                mock.patch.object(comic, "run", fake_run), \
                mock.patch.object(comic_api.comic_quota, "limits",
                                  lambda q: {"max_panels": 99}), \
                mock.patch.object(comic_api.comic_quota, "charged",
                                  lambda *a, **k: True):
            rv = self.c.post("/api/comic/start",
                             json={"task": "t", "exclusive": True})
            if "fn" in spawned:
                spawned["fn"]()                  # 直接调，省掉线程
        return rv.get_json(), run_calls

    def test_owner_can_use_exclusive(self):
        d, calls = self._start(None)             # 站主直连：qq 为空
        self.assertTrue(d.get("ok"))
        self.assertTrue(d.get("exclusive"))
        self.assertTrue(calls[0].get("exclusive"))

    def test_group_user_exclusive_is_ignored(self):
        d, calls = self._start("12345")          # 群友：qq 非空
        self.assertTrue(d.get("ok"))
        self.assertFalse(d.get("exclusive"), "群友传 exclusive 必须被忽略")
        self.assertFalse(calls[0].get("exclusive"))


class FriendPageHideTest(unittest.TestCase):
    """群友页隐藏「独占通道」勾选框（DOM 保留、CSS 隐藏）。"""

    def test_friend_page_hides_exclusive_label(self):
        with open(os.path.join(_ROOT, "web", "comic_studio.html"),
                  encoding="utf-8") as f:
            host = f.read()
        with open(os.path.join(_ROOT, "web", "comic_studio_friend.html"),
                  encoding="utf-8") as f:
            friend = f.read()
        self.assertIn("exclusiveOn", host, "母本必须有独占勾选框")
        self.assertIn("exclusiveOn", friend, "派生页 DOM 保留（JS 按 id 取值不炸）")
        self.assertIn("label:has(> #exclusiveOn)", friend,
                      "派生页必须用 CSS 隐藏独占勾选框")


if __name__ == "__main__":
    unittest.main()
