# -*- coding: utf-8 -*-
"""跨进程待跑计数（2026-11-25 方案 A）。

用户的原话：「网页端 3 张在排队，QQ 端又发一张，那 QQ 就能看到前面还有 3 张
在排。我不在乎前面那 3 张是网页端的还是 QQ 端的，我只需要知道一个准确的数。」

所以这批测试钉的不是「谁先跑」（那本来就是抢锁，谁快谁先，公平），而是
**报出去的数字准不准**：

  1. 本进程加减对得上，减到 0 时文件消失（不留 0 张的空票）；
  2. `pending_others()` 只算别人、不算自己（算了就重复计数）；
  3. `ahead_of()` = 本进程队列位置 + 别的进程待跑数；
  4. NAI 通道不参与（走云端，不抢 ComfyUI 锁）；
  5. 僵尸计数会清掉（进程被强杀时文件不会自己消失）；
  6. **加了就一定减**：`_finish` 走几条不同的路径（成功 / 失败 / 手搓 Job）
     都不能漏减，也不能重复减。

⚠️ 和 test_comfy_gate.py 一样，会动 `comfy_gate.pending_dir()`，所以只敢在
unittest 进程里跑——`config.state_path` 那里恒走临时目录，线上正被 QQ 机器人
持着的 `state/comfy.pending/` 不会被碰。

只跑本文件：python -m unittest discover -s tests -p "test_comfy_pending.py"
"""

import os
import time
import unittest
from unittest import mock

from app.features.image import comfy_gate
from app.features.image import jobs as image_jobs


def _clear_dir(path):
    try:
        for name in os.listdir(path):
            os.remove(os.path.join(path, name))
        os.rmdir(path)
    except OSError:
        pass


class PendingCounterTest(unittest.TestCase):
    """计数器本身的行为。"""

    def setUp(self):
        _clear_dir(comfy_gate.pending_dir())
        comfy_gate._pending_path[0] = ""
        comfy_gate._pending_path_backup = None

    def tearDown(self):
        _clear_dir(comfy_gate.pending_dir())
        comfy_gate._pending_path[0] = ""

    def _fake_other(self, n, pid=999999, beat_offset=0.0):
        """伪造「另一个进程排着 n 张」。beat_offset>0 = 心跳已经停了这么久。"""
        os.makedirs(comfy_gate.pending_dir(), exist_ok=True)
        fp = os.path.join(comfy_gate.pending_dir(), "%08d-deadbeef.cnt" % pid)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(str(n))
        if beat_offset:
            old = time.time() - beat_offset
            os.utime(fp, (old, old))
        return fp

    def test_add_and_release(self):
        self.assertEqual(comfy_gate.pending_mine(), 0)
        self.assertEqual(comfy_gate.add_pending(3), 3)
        self.assertEqual(comfy_gate.pending_mine(), 3)
        self.assertEqual(comfy_gate.release_pending(1), 2)
        self.assertEqual(comfy_gate.pending_mine(), 2)

    def test_zero_removes_the_file(self):
        """减到 0 要把文件删掉，别在目录里留一个「0 张」的空票。"""
        comfy_gate.add_pending(1)
        # ⚠️ 用 `_pending_file()` 拼完整路径：`_pending_path[0]` 现在只存文件名。
        self.assertTrue(os.path.isfile(comfy_gate._pending_file()))
        comfy_gate.release_pending(1)
        self.assertFalse(os.path.isfile(comfy_gate._pending_file()))
        self.assertEqual(comfy_gate.pending_total(), 0)

    def test_others_excludes_me(self):
        """⚠️ 这条最容易写错：把自己也算进去，`ahead` 就重复计数了。"""
        comfy_gate.add_pending(2)
        self._fake_other(3)
        self.assertEqual(comfy_gate.pending_others(), 3, "别人 3 张")
        self.assertEqual(comfy_gate.pending_total(), 5, "总共 5 张")

    def test_never_goes_negative(self):
        """减多了按 0 算。负数会让别人的 ahead 变成 0，报「已经在画了」。"""
        comfy_gate.add_pending(1)
        comfy_gate.release_pending(5)
        self.assertEqual(comfy_gate.pending_mine(), 0)

    def test_release_without_add_is_harmless(self):
        """没加过就减（手搓 Job / 异常路径重入）不能炸，也不能变负。"""
        self.assertEqual(comfy_gate.release_pending(1), 0)
        self.assertEqual(comfy_gate.pending_total(), 0)

    def test_stale_count_is_ignored_and_cleaned(self):
        """进程被强杀：文件不会自己消失，别让一条死队永远挡在别人前面。"""
        fp = self._fake_other(9, beat_offset=comfy_gate.PENDING_STALE + 30)
        self.assertEqual(comfy_gate.pending_others(), 0)
        self.assertFalse(os.path.isfile(fp), "僵尸文件应该被顺手删掉")

    def test_fresh_count_is_counted(self):
        self._fake_other(4, beat_offset=comfy_gate.PENDING_STALE - 10)
        self.assertEqual(comfy_gate.pending_others(), 4)

    def test_breakdown_shows_who(self):
        """排查「到底谁在排队」：能按 pid 拆开。"""
        comfy_gate.add_pending(2)
        self._fake_other(3)
        d = comfy_gate.pending_breakdown()
        self.assertEqual(d.get(999999), 3)
        self.assertEqual(sum(d.values()), 5)

    def test_broken_file_counts_as_zero(self):
        """半截文件 / 坏内容按 0 算，不许让报数整个崩掉。"""
        os.makedirs(comfy_gate.pending_dir(), exist_ok=True)
        fp = os.path.join(comfy_gate.pending_dir(), "00009999-x.cnt")
        with open(fp, "w", encoding="utf-8") as f:
            f.write("not-a-number")
        self.assertEqual(comfy_gate.pending_others(), 0)

    def test_write_failure_does_not_raise(self):
        """目录写不进去（权限/磁盘满）时不许抛异常——它只管报数，不管出图。

        ⚠️ 别用 `os.path.join("/nonexistent", "x")` 当「写不进去」的路径：
        Windows 上它会被映射成 `C:\\nonexistent`，而当前用户对根目录有写权限，
        `makedirs` 会**真的建出来**，用例就假绿了（实测踩过）。用一个不存在的
        盘符最稳——`makedirs` 必抛 WinError 5/21。
        """
        with mock.patch.object(comfy_gate, "pending_dir",
                               return_value=os.path.join("Q:", "nope")):
            self.assertEqual(comfy_gate.add_pending(1), -1)

    def test_exit_hook_clears_my_count(self):
        """正常退出要主动摘掉——否则重启 5174 后，QQ 端还会看到「前面还有 N 张」
        一直挂到 PENDING_STALE 过期，明明那边早空了。"""
        comfy_gate.add_pending(3)
        self.assertTrue(os.path.isfile(comfy_gate._pending_file()))
        comfy_gate._drop_mine()             # atexit 挂的就是这个
        self.assertFalse(os.path.isfile(comfy_gate._pending_file()))
        self.assertEqual(comfy_gate.pending_total(), 0)


class AheadOfTest(unittest.TestCase):
    """`ahead_of` = 本进程队列位置 + 别的进程待跑数。

    这就是用户要的那个场景：网页端排 3 张，QQ 端来一张，QQ 要看到 3。
    """

    def setUp(self):
        _clear_dir(comfy_gate.pending_dir())
        comfy_gate._pending_path[0] = ""
        image_jobs._reset()
        self.chan = image_jobs._COMFY
        self._worker = image_jobs._ensure_worker
        image_jobs._ensure_worker = lambda: None

    def tearDown(self):
        image_jobs._ensure_worker = self._worker
        image_jobs._reset()
        _clear_dir(comfy_gate.pending_dir())
        comfy_gate._pending_path[0] = ""

    def _enqueue(self, skill="anima-line", target="private", target_id="10001"):
        job, reason = image_jobs.enqueue(target, target_id, {}, skill=skill)
        self.assertIsNone(reason)
        return job

    def _fake_other(self, n, pid=999999):
        os.makedirs(comfy_gate.pending_dir(), exist_ok=True)
        fp = os.path.join(comfy_gate.pending_dir(), "%08d-deadbeef.cnt" % pid)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(str(n))
        return fp

    def test_enqueue_increments_pending(self):
        self._enqueue()
        self.assertEqual(comfy_gate.pending_mine(), 1)
        self._enqueue(target_id="10002")
        self.assertEqual(comfy_gate.pending_mine(), 2)

    def test_ahead_counts_other_process(self):
        """核心用例：别的进程排 3 张 → 我这张 ahead=3，不能是 0。"""
        self._fake_other(3)
        job = self._enqueue()
        self.assertEqual(image_jobs.ahead_of(job), 3)

    def test_ahead_adds_my_own_queue_position(self):
        """我先排 2 张，别人还有 3 张 → 第 3 张的 ahead = 2 + 3 = 5。

        `pending_others()` 不算自己，自己的位置单独数，所以不会重复。
        """
        self._enqueue(target_id="10001")
        self._enqueue(target_id="10002")
        self._fake_other(3)
        third = self._enqueue(target_id="10003")
        self.assertEqual(image_jobs.ahead_of(third), 5)

    def test_ahead_by_tag_reads_queue_position(self):
        """按编号查（HTTP 层只有编号，没有 Job 对象）：排第二的报 1。"""
        first = self._enqueue(target_id="10001")
        second = self._enqueue(target_id="10002")
        self.assertEqual(image_jobs.ahead_by_tag(first.tag, "anima-line"), 0)
        self.assertEqual(image_jobs.ahead_by_tag(second.tag, "anima-line"), 1)
        self._fake_other(3)
        self.assertEqual(image_jobs.ahead_by_tag(second.tag, "anima-line"), 4)

    def test_ahead_by_tag_unknown_tag_is_queue_depth(self):
        """查不到的编号 = worker 还没把它取走（工作台「正在提交」就停在这一段）。

        这时它排在**所有现存任务后面**，所以要给队列总量 + 别的进程，
        不能给 0——给 0 前端就会说「正在跑」，用户看不到前面还压着几张。
        """
        self._enqueue(target_id="10001")
        self._enqueue(target_id="10002")
        self._fake_other(3)
        self.assertEqual(
            image_jobs.ahead_by_tag("GP-not-yet-queued", "anima-line"), 5)

    def test_ahead_by_tag_nai_ignores_local_queue(self):
        """NAI 走云端不抢锁：本地压着 5 张也不该算进它的前面。"""
        self._enqueue(target_id="10001")
        self._enqueue(target_id="10002")
        self._fake_other(5)
        nai = self._enqueue(skill="nai", target="private", target_id="20001")
        self.assertEqual(image_jobs.ahead_by_tag(nai.tag, "nai"), 0)

    def test_ahead_by_tag_empty_is_none(self):
        """没编号就老实说不知道，别拿 0 冒充「不用等」。"""
        self.assertIsNone(image_jobs.ahead_by_tag("", "anima-line"))

    def test_nai_is_not_affected(self):
        """NAI 走云端不抢锁：本地压着 5 张也不该让 NAI 报「前面还有 5 张」。"""
        self._fake_other(5)
        nai = self._enqueue(skill="nai", target="private", target_id="20001")
        self.assertEqual(image_jobs.ahead_of(nai), 0)

    def test_nai_does_not_register_pending(self):
        """NAI 不占 ComfyUI，就不该进计数——进去了会让本地用户看到虚高的数字。"""
        self.assertEqual(comfy_gate.pending_mine(), 0)
        self._enqueue(skill="nai", target="private", target_id="20002")
        self.assertEqual(comfy_gate.pending_mine(), 0)

    def test_finish_releases_pending(self):
        """跑完一张，计数要跟着减回去。"""
        job = self._enqueue()
        self.assertEqual(comfy_gate.pending_mine(), 1)
        image_jobs._finish(job)
        self.assertEqual(comfy_gate.pending_mine(), 0)

    def test_finish_twice_does_not_double_release(self):
        """`_finish` 被同一个 Job 调两次时不许减两次——减多了会报「已经在画了」。"""
        self._enqueue(target_id="10001")     # 留一张垫底，才看得出多减
        job = self._enqueue(target_id="10002")
        self.assertEqual(comfy_gate.pending_mine(), 2)
        image_jobs._finish(job)
        image_jobs._finish(job)
        self.assertEqual(comfy_gate.pending_mine(), 1, "只该减一次")

    def test_finish_after_error_still_releases(self):
        """失败 / 超时也要减——只增不减的话别人永远看到「前面还有 N 张」。"""
        job = self._enqueue()
        job.error = RuntimeError("模拟失败")
        image_jobs._finish(job)
        self.assertEqual(comfy_gate.pending_mine(), 0)

    def test_rejected_enqueue_does_not_count(self):
        """被拒掉的单子根本没进队列，不能占一个名额。

        2026-11-25 起没有时段限制，`inflight_limit()` 恒返回 5。要模拟「满了」
        得让前面那张先占住名额——上限是通道建的时候拍下来的（`chan.max_inflight`），
        这里把它临时压到 1，等价于「这个会话只剩 0 个空位」。
        """
        self._enqueue(target_id="10001")
        chan = image_jobs._channel_of("anima-line")
        old = chan.max_inflight
        try:
            chan.max_inflight = 1          # 这个会话只剩 0 个空位
            job, reason = image_jobs.enqueue("private", "10001", {},
                                             skill="anima-line")
        finally:
            chan.max_inflight = old
        self.assertIsNone(job)
        self.assertTrue(reason)
        self.assertEqual(comfy_gate.pending_mine(), 1, "只有第一张算数")


if __name__ == "__main__":
    unittest.main()
