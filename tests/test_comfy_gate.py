# -*- coding: utf-8 -*-
"""跨进程 ComfyUI 闸门（2026-10-18）：漫画工作台和 QQ 机器人排队用同一张卡。

背景：两个进程（Flask 网页 / bot）各有队列，都直连 ComfyUI。「连画 N 张就整机
重启」「内存水位过低重启」这两条各自为政，于是这边一重启就把对方正在跑的那格
掐断（用户实录「漫画任务突然结束」）。这份测试钉死四件事：

  1. hold() 拿到锁 → 目录存在、busy_others() 认「是自己的」不算别人；
     释放后目录消失；
  2. 别的进程持锁 → busy_others() 为真，等锁超时抛 TimeoutError（不无限等）；
  3. 心跳停超过 STALE 秒 = 僵尸锁，可以被抢（进程被强杀不会锁死全场）；
  4. 释放前比 token：锁已经被别人抢走时绝不去删别人的；
  5. 排队号（2026-10-26）：只有队首能 mkdir、先登记的先进、票的僵尸也会清、
     释放（含「锁已易主」这条路）一定会摘票——不摘就把整条队卡死。

顺带钉住漫画批量的重启阈值：`batch="comic"` 走 COMFY_RELEASE_AFTER_COMIC（6），
普通单张仍走全局 total（2）。

⚠️ 这些用例会清 `comfy_gate.lock_dir()`，**只敢在临时目录里跑**——
`config.state_path` 在 unittest 进程里恒走 `tempfile.mkdtemp()`，所以线上
`state/comfy.lock`（正被 QQ 机器人持有）不会被碰。别把 state_path 换回真目录。

只跑本文件：python -m unittest discover -s tests -p "test_comfy_gate.py"
"""

import json
import os
import threading
import time
import unittest
from unittest import mock

from app import comfy_gate, image_jobs


def _clear_dir(path):
    """删掉 state 下的锁/票目录（含里面的文件）。"""
    try:
        for name in os.listdir(path):
            os.remove(os.path.join(path, name))
        os.rmdir(path)
    except OSError:
        pass


class GateTest(unittest.TestCase):
    def setUp(self):
        self.lock = comfy_gate.lock_dir()
        self._clear()
        _clear_dir(comfy_gate.queue_dir())

    def tearDown(self):
        self._clear()
        _clear_dir(comfy_gate.queue_dir())

    def _clear(self):
        try:
            fp = os.path.join(self.lock, "owner.json")
            if os.path.isfile(fp):
                os.remove(fp)
            os.rmdir(self.lock)
        except OSError:
            pass

    def _fake_other(self, beat_offset=0.0, pid=999999, label="漫画工作台 格01"):
        """伪造「另一个进程正持锁」。"""
        os.makedirs(self.lock, exist_ok=True)
        with open(os.path.join(self.lock, "owner.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"pid": pid, "token": "someone-else", "label": label,
                       "since": time.time() - 60,
                       "beat": time.time() - beat_offset}, f)

    def test_hold_creates_lock_and_release_removes_it(self):
        with comfy_gate.hold("单元测试"):
            self.assertTrue(os.path.isdir(self.lock))
            self.assertTrue(os.path.isfile(os.path.join(self.lock, "owner.json")))
            # 自己拿着不算「别人在跑」——提交前的重启决策要放行
            self.assertFalse(comfy_gate.busy_others())
        self.assertFalse(os.path.isdir(self.lock))
        # 票也要跟着走：留着就成了队首，别人永远排不上
        self.assertEqual(comfy_gate._tickets(), [])

    def test_other_process_holder_is_seen(self):
        self._fake_other()
        self.assertTrue(comfy_gate.busy_others())
        self.assertIn("漫画工作台", comfy_gate.describe(comfy_gate.holder()))

    def test_wait_times_out_when_held_by_other(self):
        self._fake_other()
        t0 = time.time()
        with self.assertRaises(TimeoutError):
            comfy_gate.hold("单元测试", timeout=1.2)
        self.assertLess(time.time() - t0, 10)      # 真的会放弃，不是死等

    def test_stale_lock_can_be_taken(self):
        """进程被强杀：心跳停了，锁不该把全场锁死。"""
        self._fake_other(beat_offset=comfy_gate.STALE + 10)
        self.assertIsNone(comfy_gate.holder())
        with comfy_gate.hold("单元测试", timeout=3):
            self.assertFalse(comfy_gate.busy_others())

    def test_release_does_not_delete_someone_elses_lock(self):
        g = comfy_gate.hold("单元测试")
        self._fake_other()          # 模拟：自己的锁被判僵尸、别人抢走了
        g.release()
        self.assertTrue(os.path.isdir(self.lock))
        self.assertTrue(comfy_gate.busy_others())
        # 但票必须摘掉——不然自己永远排在队首，谁也别想拿锁
        self.assertEqual(comfy_gate._tickets(), [])

    def test_no_lock_means_not_busy(self):
        self.assertFalse(comfy_gate.busy_others())
        self.assertIsNone(comfy_gate.holder())


class QueueFairnessTest(unittest.TestCase):
    """排队号（2026-10-26）：修的是「网页侧等了 12 分钟一次没抢到」。

    纯抢占下 QQ 的 worker 是「释放 → 立刻取下张 → mkdir」，毫秒级；网页侧
    每秒才试一次，只要对方队列不断粮就永远轮不到。这里钉三件事：非队首抢不到
    / 僵尸票会被清 / 先登记的先进。
    """

    def setUp(self):
        _clear_dir(comfy_gate.lock_dir())
        _clear_dir(comfy_gate.queue_dir())

    def tearDown(self):
        _clear_dir(comfy_gate.lock_dir())
        _clear_dir(comfy_gate.queue_dir())

    def _fake_other(self, pid=999999, label="漫画工作台 格01"):
        """伪造「另一个进程正持锁」（心跳是新的，不算僵尸）。"""
        os.makedirs(comfy_gate.lock_dir(), exist_ok=True)
        with open(os.path.join(comfy_gate.lock_dir(), "owner.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"pid": pid, "token": "someone-else", "label": label,
                       "since": time.time() - 60, "beat": time.time()}, f)

    def _fake_ticket(self, age_ns=-1_000_000_000):
        """造一张票。age_ns 为负 = 比我更早登记（排我前面）。"""
        os.makedirs(comfy_gate.queue_dir(), exist_ok=True)
        fp = os.path.join(comfy_gate.queue_dir(),
                          "%020d-00009999-fakefake.ticket"
                          % int(time.time_ns() + age_ns))
        with open(fp, "w", encoding="utf-8"):
            pass
        return fp

    def test_not_head_does_not_take_lock(self):
        """不是队首就一个 mkdir 都不许发——这正是老代码饿死的根。"""
        self._fake_ticket(age_ns=-5_000_000_000)        # 别人比我早 5 秒
        t0 = time.time()
        with self.assertRaises(TimeoutError):
            comfy_gate.hold("单元测试", timeout=1.2)
        self.assertFalse(os.path.isdir(comfy_gate.lock_dir()))
        self.assertLess(time.time() - t0, 10)
        # 超时退出也得摘自己的票：不然这张死票继续当队首，后面全卡死
        self.assertEqual(len(comfy_gate._tickets()), 1)

    def test_stale_ticket_is_swept(self):
        """等的人被强杀留下的票，不能把锁挡到天荒地老。"""
        fp = self._fake_ticket(age_ns=-5_000_000_000)
        old = time.time() - (comfy_gate.TICKET_STALE + 30)
        os.utime(fp, (old, old))
        with comfy_gate.hold("单元测试", timeout=5):
            self.assertTrue(os.path.isdir(comfy_gate.lock_dir()))

    def test_earlier_ticket_wins_when_lock_frees(self):
        """锁一空，先登记的那个先跑——这就是「不被饿死」的行为保证。"""
        self._fake_other()
        got = []

        def worker(tag):
            with comfy_gate.hold(tag, timeout=20):
                got.append(tag)
                time.sleep(0.3)

        first = threading.Thread(target=worker, args=("先",))
        first.start()
        time.sleep(0.4)                     # 「先」登记好票、进入等待
        second = threading.Thread(target=worker, args=("后",))
        second.start()
        time.sleep(0.6)                     # 「后」也登记好、进入等待
        _clear_dir(comfy_gate.lock_dir())   # 放开那把假锁
        first.join(25)
        second.join(25)
        self.assertEqual(got, ["先", "后"])

    def test_holder_ticket_is_kept_alive_by_heartbeat(self):
        """持锁跑长图（两三分钟）时票不能变僵尸，否则别人会当它是死票。

        把 STALE 压到 1 秒、心跳压到 0.2 秒来在测试时长内验证：心跳在刷，
        `_sweep_tickets()` 就不许动它。
        """
        with mock.patch.object(comfy_gate, "HEARTBEAT", 0.2), \
                mock.patch.object(comfy_gate, "TICKET_STALE", 1.0):
            with comfy_gate.hold("单元测试", timeout=5):
                mine = comfy_gate._tickets()
                self.assertEqual(len(mine), 1)
                time.sleep(1.5)             # 超过 STALE，靠心跳续命
                comfy_gate._sweep_tickets()
                survived = comfy_gate._tickets()
                self.assertEqual(survived, mine)

    def test_waiter_ticket_is_kept_alive_while_waiting(self):
        """排队等两分钟也不能被自己人的 sweep 清掉——清了就永远轮不到。"""
        self._fake_other()
        with mock.patch.object(comfy_gate, "TICKET_STALE", 1.0):
            def waiter():
                with self.assertRaises(TimeoutError):
                    comfy_gate.hold("排队的", timeout=2.0)

            t = threading.Thread(target=waiter)
            t.start()
            time.sleep(0.5)
            mine = [n for n in comfy_gate._tickets()]
            self.assertEqual(len(mine), 1, "排队中应该正好一张票")
            time.sleep(1.0)                 # 超过 STALE
            comfy_gate._sweep_tickets()
            self.assertEqual(len(comfy_gate._tickets()), 1, "排队方的票被误清了")
            t.join(15)


class ComicBatchLimitTest(unittest.TestCase):
    """漫画批量换阈值：COMFY_RELEASE_AFTER_COMIC（6）而不是全局 total（2）。"""

    def setUp(self):
        self.chan = image_jobs._COMFY
        self._saved = {k: getattr(self.chan, k)
                       for k in ("total_run", "same_run", "last_skill")}
        self.chan.same_run = 1
        self.chan.last_skill = "anima-line"

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.chan, k, v)

    def _job(self, batch=None):
        return image_jobs.Job(None, None, {}, skill="anima-line", batch=batch)

    def _run(self):
        with mock.patch.object(image_jobs, "_restart_comfy") as rs, \
                mock.patch.object(image_jobs, "_report_and_free") as fr, \
                mock.patch.object(image_jobs, "COMFY_RELEASE_AFTER_TOTAL", 2), \
                mock.patch.object(image_jobs, "COMFY_RELEASE_AFTER_COMIC", 6):
            image_jobs._maybe_release_for_switch(self._job(batch="comic"))
            return rs, fr

    def test_comic_batch_does_not_restart_before_six(self):
        self.chan.total_run = 5           # 普通单张早就该重启了
        rs, fr = self._run()
        rs.assert_not_called()
        fr.assert_not_called()            # 也不用退而求其次打 /free
        self.assertEqual(self.chan.total_run, 6)

    def test_comic_batch_restarts_at_six(self):
        self.chan.total_run = 6
        rs, _fr = self._run()
        rs.assert_called_once()
        self.assertEqual(self.chan.total_run, 1)   # 重启后这张算新一轮第 1 张

    def test_normal_job_still_uses_global_total(self):
        self.chan.total_run = 2
        with mock.patch.object(image_jobs, "_restart_comfy") as rs, \
                mock.patch.object(image_jobs, "_report_and_free"), \
                mock.patch.object(image_jobs, "COMFY_RELEASE_AFTER_TOTAL", 2), \
                mock.patch.object(image_jobs, "COMFY_RELEASE_AFTER_COMIC", 6):
            image_jobs._maybe_release_for_switch(self._job())
        rs.assert_called_once()


class RestartAvoidanceTest(unittest.TestCase):
    """别人在跑时绝不重启 ComfyUI——这是「不掐断漫画」的落点。"""

    def test_restart_is_skipped_when_other_process_is_running(self):
        with mock.patch.object(image_jobs.comfy_gate, "busy_others",
                               return_value=True), \
                mock.patch.object(image_jobs.requests, "get") as get:
            self.assertFalse(image_jobs._restart_comfy())
        get.assert_not_called()           # 连 /manager/reboot 都没发

    def test_ram_guard_is_skipped_when_other_process_is_running(self):
        with mock.patch.object(image_jobs.comfy_gate, "busy_others",
                               return_value=True), \
                mock.patch.object(image_jobs, "_free_ram_gb") as probe, \
                mock.patch.object(image_jobs, "_restart_comfy") as rs:
            image_jobs._maybe_restart_for_ram()
        probe.assert_not_called()         # 诊断都不做，等对方跑完再说
        rs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
