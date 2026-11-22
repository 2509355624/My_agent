"""状态后台测试（app/features/qq/status.py + image_jobs.snapshot）。

原则同 test_image_jobs：零网络、零显卡、零真实线程。
- image_jobs 的 worker 一律挡掉（_ensure_worker 成空操作），绝不让它真去
  提交 ComfyUI——enqueue 会拉起真实 worker，测试里碰它就是真机生图。
- SnowLuma 探活（qq_status._alive）mock，不打 3000 口。
- 状态文件写临时目录，不碰真实的 state/。
"""

import os
import tempfile
import time
import unittest
from unittest import mock

from app.features.image import jobs as image_jobs
from app.features.qq import status as qq_status


class _FakeRunner:
    """替身 SessionRunner：只提供 snapshot() 会读的那几个字段。"""

    def __init__(self, key, target="group", tid="1", state="idle",
                 since=None, pending=0, preview="", senders=None):
        self.session_key = key
        self.target = target
        self.target_id = tid
        self.state = state
        self.state_since = time.monotonic() if since is None else since
        self._pending = [object()] * pending
        self.turn_preview = preview
        self.batch_senders = senders or []


class ImageJobsSnapshotTest(unittest.TestCase):
    """队列快照：后台「谁在排队、排了多久」的数据来源。"""

    def setUp(self):
        image_jobs._reset()
        for target, repl in (("_ensure_worker", lambda: None),
                             ("_queue_prompt", lambda wf: "pid"),
                             ("_wait_comfy_idle", lambda timeout=90: True),
                             ("_free_vram_gb", lambda: None)):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def test_empty_queue(self):
        s = image_jobs.snapshot()
        self.assertIsNone(s["running"])
        self.assertEqual(s["queued"], [])
        self.assertEqual(s["depth"], 0)

    def test_exposes_who_and_how_long(self):
        image_jobs.enqueue("group", "111", "aaa", skill="anima_soft")
        image_jobs.enqueue("group", "222", "bbb", skill="anima_soft")
        s = image_jobs.snapshot()
        self.assertEqual(s["depth"], 2)
        ids = [q["target_id"] for q in s["queued"]]
        self.assertIn("222", ids)
        for q in s["queued"]:
            self.assertEqual(q["skill"], "anima_soft")
            self.assertIn("ahead", q)
            self.assertIn("age", q)
            self.assertGreaterEqual(q["age"], 0)

    def test_prompt_is_truncated(self):
        image_jobs.enqueue("group", "1", "x" * 200, skill="anima_soft")
        for q in image_jobs.snapshot()["queued"]:
            self.assertLessEqual(len(q["prompt"]), 60)

    def test_web_jobs_carry_source_label(self):
        """快照每行带 src 标签（2026-11-22）：状态页要区分 moss漫画/漫画台/网页。"""
        image_jobs.enqueue("group", "9", "qq", skill="anima_soft")
        image_jobs.enqueue(None, None, "m", skill="anima_soft",
                           out_dir="web/moss漫画/20261122_1956_粉衣少女与异界门")
        image_jobs.enqueue(None, None, "c", skill="anima_soft",
                           batch="comic", out_dir="web/漫画_20261122_x")
        image_jobs.enqueue(None, None, "g", skill="anima_soft")
        image_jobs.enqueue(None, None, "o", skill="anima_soft", out_dir="换装")
        srcs = {q["prompt"]: q["src"] for q in image_jobs.snapshot()["queued"]}
        self.assertEqual(srcs["qq"], "QQ")
        self.assertEqual(srcs["m"], "moss漫画")
        self.assertEqual(srcs["c"], "漫画台")
        self.assertEqual(srcs["g"], "网页")
        self.assertEqual(srcs["o"], "换装")


class StatusPageWebQueueTest(unittest.TestCase):
    """状态页 / /api/status 必须带上网页端进程内队列（2026-11-22）。"""

    @staticmethod
    def _read(*parts):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, *parts), encoding="utf-8") as f:
            return f.read()

    def test_status_page_renders_web_queue(self):
        html = self._read("web", "status.html")
        self.assertIn("tbodyWebJobs", html)
        self.assertIn("renderWebJobs", html)
        self.assertIn("网页端队列空闲", html)

    def test_api_status_exposes_web_snapshot(self):
        src = self._read("app", "main.py")
        self.assertIn("image_jobs.snapshot()", src)
        self.assertIn('"web": web', src)


class QqStatusTest(unittest.TestCase):
    """状态落盘 / 读取 / 过期判定。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "qq_status.json")
        p = mock.patch.object(qq_status, "_alive", lambda: (True, "测试号"))
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _bot(runners):
        return mock.Mock(runners=runners)

    def test_write_then_read_roundtrip(self):
        bot = self._bot({"group:1": _FakeRunner("group:1", state="waiting_slot")})
        self.assertTrue(qq_status.write_snapshot(bot, self.path))
        data, stale = qq_status.read(self.path)
        self.assertFalse(stale)
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(data["sessions"][0]["state"], "waiting_slot")
        self.assertEqual(data["sessions"][0]["state_label"], "排队等并发槽")

    def test_replace_retries_a_sharing_violation(self):
        """Flask 读快照的那一瞬间，os.replace 会吃 WinError 5。

        实测两天撞了 84 次。对方的句柄只活到 json.load 读完，退让几毫秒重试
        就该过去——重试不该让整次写盘失败（那会让后台显示「无心跳」）。
        """
        real = os.replace
        attempts = []

        def flaky(src, dst):
            if dst == self.path:
                attempts.append(1)
                if len(attempts) == 1:
                    raise PermissionError(5, "拒绝访问")
            return real(src, dst)

        with mock.patch.object(qq_status.os, "replace", flaky):
            self.assertTrue(qq_status.write_snapshot(self._bot({}), self.path))
        self.assertEqual(len(attempts), 2)      # 第一次撞上，第二次过
        data, stale = qq_status.read(self.path)
        self.assertFalse(stale)
        self.assertEqual(data["sessions"], [])

    def test_write_reports_failure_when_still_locked(self):
        """重试完还锁着就老实返回 False（调用方只记日志，不拖垮 bot）。"""
        with mock.patch.object(qq_status.os, "replace",
                               side_effect=PermissionError(5, "拒绝访问")):
            self.assertFalse(qq_status.write_snapshot(self._bot({}), self.path))

    def test_read_missing_file_is_stale(self):
        data, stale = qq_status.read(os.path.join(self.tmp, "nope.json"))
        self.assertIsNone(data)
        self.assertTrue(stale)

    def test_stale_when_mtime_is_old(self):
        """mtime 停更 = bot 卡死。这是后台标红「无心跳」的判据。"""
        qq_status.write_snapshot(self._bot({}), self.path)
        old = time.time() - 100
        os.utime(self.path, (old, old))
        _, stale = qq_status.read(self.path)
        self.assertTrue(stale)

    def test_waiting_sessions_sort_first(self):
        """等并发槽的排最前——那才是「我以为卡了」的人。"""
        now = time.monotonic()
        bot = self._bot({
            "group:1": _FakeRunner("group:1", tid="1", state="idle", since=now),
            "group:2": _FakeRunner("group:2", tid="2",
                                   state="waiting_slot", since=now - 5),
            "group:3": _FakeRunner("group:3", tid="3", state="running",
                                   since=now - 1),
        })
        s = qq_status.snapshot(bot)
        self.assertEqual([x["state"] for x in s["sessions"]],
                         ["waiting_slot", "running", "idle"])
        # 时长用 monotonic 相减，必须是正数（拿 time.time() 减会得垃圾）
        self.assertGreaterEqual(s["sessions"][0]["for"], 4)

    def test_protocol_and_last_activity_present(self):
        s = qq_status.snapshot(self._bot({}))
        self.assertTrue(s["protocol"]["online"])
        self.assertIn("last_activity_ago", s)


class SessionRunnerStateTest(unittest.TestCase):
    """锁住 SessionRunner 的阶段字段——后台全靠它们显示「谁在排队」。"""

    def test_runner_starts_idle(self):
        from app import qq_bot
        r = qq_bot.SessionRunner(mock.Mock(), "group:1", "group", "1")
        self.assertEqual(r.state, "idle")
        self.assertIsNotNone(r.state_since)
        self.assertEqual(r.turn_preview, "")
        self.assertEqual(r.batch_senders, [])
