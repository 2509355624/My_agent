# -*- coding: utf-8 -*-
"""跨进程 ComfyUI 闸门（2026-10-18）：漫画工作台和 QQ 机器人排队用同一张卡。

背景：两个进程（Flask 网页 / bot）各有队列，都直连 ComfyUI。「连画 N 张就整机
重启」「内存水位过低重启」这两条各自为政，于是这边一重启就把对方正在跑的那格
掐断（用户实录「漫画任务突然结束」）。这份测试钉死四件事：

  1. hold() 拿到锁 → 目录存在、busy_others() 认「是自己的」不算别人；
     释放后目录消失；
  2. 别的进程持锁 → busy_others() 为真，等锁超时抛 TimeoutError（不无限等）；
  3. 心跳停超过 STALE 秒 = 僵尸锁，可以被抢（进程被强杀不会锁死全场）；
  4. 释放前比 token：锁已经被别人抢走时绝不去删别人的。

顺带钉住漫画批量的重启阈值：`batch="comic"` 走 COMFY_RELEASE_AFTER_COMIC（6），
普通单张仍走全局 total（2）。

只跑本文件：python -m unittest discover -s tests -p "test_comfy_gate.py"
"""

import json
import os
import time
import unittest
from unittest import mock

from app import comfy_gate, image_jobs


class GateTest(unittest.TestCase):
    def setUp(self):
        self.lock = comfy_gate.lock_dir()
        self._clear()

    def tearDown(self):
        self._clear()

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

    def test_no_lock_means_not_busy(self):
        self.assertFalse(comfy_gate.busy_others())
        self.assertIsNone(comfy_gate.holder())


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
