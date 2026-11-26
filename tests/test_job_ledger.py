#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务账本 + 重启补发（app/job_ledger.py + image_jobs.resume_pending）。

要钉住的行为（2026-10-17 用户提的「重启后任务能不能继续」）：

1. **提交成功才记账**：缺 tag / prompt_id 记不进去；quiet（扮演态配图）和
   NAI（云端）压根不记 —— 这两种重启后没有东西可捞。
2. **终结必划账**：`_finish` 是所有路径的收口，漏了它重启后会去追一张早就
   发完的图。
3. **重启补发三条分支**：
   - /history 有结果 → 取图补发，**不重画**（最值钱的一段）；
   - 还在 /queue 里 → 等它跑完再取；
   - 两边都没有 → ComfyUI 也重启过，只能老实说一句「没画成」；
   - 另外「问不到 ComfyUI」必须留着下次再试，不许当成任务没了。

零网络、零显卡、零真实线程等待。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app.features.image import job_ledger
from app.features.image import jobs as image_jobs
from app.features.image import log as image_log


class _TempLedger(unittest.TestCase):
    """每个用例一份独立账本文件（别碰真实 state/）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jobs_ledger_test_")
        self.path = os.path.join(self.dir, "pending_jobs.json")
        self._patch = mock.patch.object(job_ledger, "PATH", self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


class LedgerFileTest(_TempLedger):
    def test_roundtrip(self):
        rec = {"tag": "HT-1", "prompt_id": "pid-1", "target": "private",
               "target_id": "42", "skill": "silver", "prompt": "1girl"}
        self.assertTrue(job_ledger.add(rec))
        self.assertEqual(job_ledger.load()["HT-1"]["prompt_id"], "pid-1")
        self.assertTrue(job_ledger.remove("HT-1"))
        self.assertEqual(job_ledger.load(), {})

    def test_missing_ids_are_rejected(self):
        self.assertFalse(job_ledger.add({"prompt_id": "pid"}))       # 没 tag
        self.assertFalse(job_ledger.add({"tag": "HT-1"}))            # 没 prompt_id
        self.assertEqual(job_ledger.load(), {})

    def test_broken_file_reads_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 JSON")
        self.assertEqual(job_ledger.load(), {})      # 坏账本不许拦住启动

    def test_remove_is_idempotent(self):
        self.assertTrue(job_ledger.remove("HT-none"))
        job_ledger.add({"tag": "HT-1", "prompt_id": "pid-1"})
        job_ledger.remove("HT-1")
        self.assertTrue(job_ledger.remove("HT-1"))

    def test_clear(self):
        job_ledger.add({"tag": "HT-1", "prompt_id": "pid-1"})
        job_ledger.clear()
        self.assertEqual(job_ledger.load(), {})


class LedgerHookTest(_TempLedger):
    """账本挂在哪儿：提交后写、_finish 划掉、quiet/NAI 不写。"""

    def _job(self, **kw):
        kw.setdefault("skill", "silver")
        job = image_jobs.Job("private", "42", {"1": {}}, **kw)
        job.prompt_id = "pid-1"
        return job

    def test_submit_records_the_job(self):
        job = self._job()
        image_jobs._ledger_add(job)
        rec = job_ledger.load()[job.tag]
        self.assertEqual(rec["prompt_id"], "pid-1")
        self.assertEqual(rec["target_id"], "42")
        self.assertEqual(rec["skill"], "silver")

    def test_quiet_and_nai_are_not_recorded(self):
        image_jobs._ledger_add(self._job(quiet=True))
        image_jobs._ledger_add(self._job(skill="nai"))
        self.assertEqual(job_ledger.load(), {})

    def test_no_prompt_id_is_not_recorded(self):
        job = self._job()
        job.prompt_id = None
        image_jobs._ledger_add(job)
        self.assertEqual(job_ledger.load(), {})

    def test_finish_removes_the_record(self):
        job = self._job()
        image_jobs._ledger_add(job)
        self.assertIn(job.tag, job_ledger.load())
        image_jobs._finish(job)
        self.assertEqual(job_ledger.load(), {})


class ResumeTest(_TempLedger):
    """重启补发：三条分支 + 问不到时留着。"""

    TAG = "HT-20261017-140000-001"

    def setUp(self):
        super().setUp()
        self.rec = {"tag": self.TAG, "prompt_id": "pid-1",
                    "target": "private", "target_id": "42",
                    "skill": "silver", "prompt": "1girl", "seed": 7}
        # 恢复闩是模块级的，用例之间要清掉
        self._resumed = mock.patch.object(image_jobs, "_resumed", False)
        self._resumed.start()

    def tearDown(self):
        self._resumed.stop()
        super().tearDown()

    def _run(self, entry=None, in_queue=False, send=None, polled=True):
        with mock.patch.object(image_jobs, "poll_once",
                               return_value=(entry if polled else None)), \
             mock.patch.object(image_jobs, "_comfy_queue_has",
                               return_value=in_queue), \
             mock.patch.object(image_jobs, "wait_done",
                               return_value=entry) as m_wait, \
             mock.patch.object(image_jobs, "_send_image",
                               side_effect=send or (lambda *a, **k: True)) as m_send, \
             mock.patch.object(image_jobs, "_send_text") as m_text, \
             mock.patch.object(image_jobs.image_log, "save") as m_save:
            done = image_jobs._resume_one(self.TAG, dict(self.rec))
        return done, m_send, m_text, m_wait, m_save

    def test_history_result_is_delivered_without_redrawing(self):
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        done, m_send, m_text, m_wait, m_save = self._run(entry=entry)
        self.assertTrue(done)
        m_send.assert_called_once()
        # 2026-10-18 起图按会话分目录，补发时按账本里的会话+渠道补出目录；
        # 万一是老版本提交的单（图还平铺在根目录），`prepare_for_send` 会
        # 在 404 时退回裸文件名再取一次。
        self.assertEqual(m_send.call_args[0][2], "qq/私聊_42/silver/a.png")
        self.assertEqual(m_send.call_args[0][3], self.TAG)
        m_wait.assert_not_called()          # 已经有结果了，不用等
        m_text.assert_not_called()          # 更不许说「没画成」
        m_save.assert_called_once()         # 编号 → 提示词照样记账

    def test_job_still_in_queue_is_waited_for(self):
        entry = {"outputs": {"9": {"images": [{"filename": "b.png"}]}}}
        done, m_send, _, m_wait, _ = self._run(entry=entry, in_queue=True,
                                              polled=False)
        self.assertTrue(done)
        m_wait.assert_called_once()
        m_send.assert_called_once()

    def test_gone_from_comfy_tells_the_user(self):
        done, m_send, m_text, _, _ = self._run(entry=None, in_queue=False)
        self.assertTrue(done)
        m_send.assert_not_called()
        m_text.assert_called_once()
        self.assertIn("重启", m_text.call_args[0][2])

    def test_multi_image_all_blocked_does_not_retry(self):
        """一批漫画全被审核拦下：**当处理完**，不再每次启动重试刷屏。

        2026-10-18 用户报的现场：重启后补发 10 张全被拦，每张回一句「这张图
        没过审」，而且 sent=0 会累加 attempts——下次启动再来一遍。
        """
        entry = {"outputs": {"9": {"images": [
            {"filename": "c%d.png" % i} for i in range(10)]}}}
        done, m_send, m_text, _, m_save = self._run(
            entry=entry, send=lambda *a, **k: False)
        self.assertTrue(done, "全被审核拦下不该留在账本里等重试")
        self.assertEqual(10, m_send.call_count)
        self.assertTrue(all(c.kwargs.get("notify") is False
                            for c in m_send.call_args_list),
                        "逐张都要静默，不然就是 10 句刷屏")
        m_text.assert_called_once()                # 10 句 → 1 句
        self.assertIn("10 张都没过审", m_text.call_args[0][2])
        m_save.assert_not_called()

    def test_multi_image_partial_blocked_gives_counts(self):
        entry = {"outputs": {"9": {"images": [
            {"filename": "d%d.png" % i} for i in range(4)]}}}
        seq = [True, False, True, False]
        done, _m_send, m_text, _, _ = self._run(
            entry=entry, send=lambda *a, **k: seq.pop(0))
        self.assertTrue(done)
        m_text.assert_called_once()
        text = m_text.call_args[0][2]
        self.assertIn("2 张没过审", text)
        self.assertIn("2 张已发", text)

    def test_unreachable_comfy_keeps_the_record(self):
        done, m_send, m_text, _, _ = self._run(entry=None, in_queue=None)
        self.assertFalse(done)              # 留着下次启动再试
        m_send.assert_not_called()
        m_text.assert_not_called()          # 不许把「问不到」当成「没了」

    def test_send_failure_keeps_the_record_and_counts_an_attempt(self):
        entry = {"outputs": {"9": {"images": [{"filename": "c.png"}]}}}
        done, _, _, _, _ = self._run(entry=entry, send=RuntimeError("协议端挂了"))
        self.assertFalse(done)

    def test_gives_up_after_max_attempts(self):
        self.rec["attempts"] = image_jobs._RESUME_MAX_ATTEMPTS - 1
        entry = {"outputs": {"9": {"images": [{"filename": "c.png"}]}}}
        done, _, _, _, _ = self._run(entry=entry,
                                     send=Exception("永远发不出去"))
        self.assertTrue(done)               # 别再无限重试

    def test_resume_pending_runs_only_once(self):
        # 先塞一条，第一次调用会启动线程；第二次直接返回 None
        job_ledger.add(self.rec)
        with mock.patch.object(image_jobs, "_resume_one",
                               return_value=True) as m_one:
            t = image_jobs.resume_pending()
            self.assertIsNotNone(t)
            t.join(5)
            self.assertIsNone(image_jobs.resume_pending())
        m_one.assert_called_once()


class QueuedLostTest(_TempLedger):
    """重启时「还在排队」的那几张：救不回来，但**必须让人知道**（2026-11-26）。

    用户报的现场：回执已经说过「正在画」，这时重启，那几张无声无息就没了，
    用户对着「正在画」等半小时。所以入队就记一条 `queued` 轻记录，重启后
    只有「留下它的进程已经不在了」才报，且**按会话汇总成一句**。
    """

    def _queued(self, tag="HT-Q1", pid=999999, target="private",
                target_id="42", skill="silver"):
        return {"tag": tag, "state": job_ledger.STATE_QUEUED,
                "pid": pid, "target": target, "target_id": target_id,
                "skill": skill, "prompt": "1girl", "created": 0.0}

    def test_queued_record_needs_no_prompt_id(self):
        # 这是和「已提交」记录唯一的区别：排队中当然没有 prompt_id
        self.assertTrue(job_ledger.add(self._queued()))
        self.assertEqual(job_ledger.load()["HT-Q1"]["state"], "queued")

    def test_submitted_record_still_requires_prompt_id(self):
        # 老规矩不能松：没有 prompt_id 的「已提交」记录捞不回来
        self.assertFalse(job_ledger.add({"tag": "HT-S1", "target": "private"}))

    def test_live_pid_record_is_kept(self):
        """另一个进程还活着（5174 起来了、QQ 还没重启）→ 一个字都不许动。"""
        job_ledger.add(self._queued(pid=12345))
        lost = job_ledger.drop_queued(lambda pid: True)
        self.assertEqual({}, lost)
        self.assertIn("HT-Q1", job_ledger.load())

    def test_dead_pid_record_is_reported_and_cleared(self):
        job_ledger.add(self._queued(pid=999999))
        lost = job_ledger.drop_queued(lambda pid: False)
        self.assertIn("HT-Q1", lost)
        self.assertEqual({}, job_ledger.load())     # 报完就清，别每次启动都报

    def test_submitted_records_are_untouchable(self):
        # 有 prompt_id 的走 resume 捞图，绝不能被当成「丢了」清掉
        job_ledger.add({"tag": "HT-S1", "prompt_id": "pid-1",
                        "target": "private", "target_id": "42"})
        lost = job_ledger.drop_queued(lambda pid: False)
        self.assertEqual({}, lost)
        self.assertIn("HT-S1", job_ledger.load())

    def test_alive_check_failure_counts_as_alive(self):
        # 判不了就当他活着：宁可少报一句，不能把马上要出图的活说成没画成
        job_ledger.add(self._queued())
        lost = job_ledger.drop_queued(lambda pid: 1 / 0)
        self.assertEqual({}, lost)

    def test_own_pid_counts_as_dead(self):
        # 自己的队列在内存里，账本里留着自己的 queued = 老进程留下的死记录
        job_ledger.add(self._queued(pid=os.getpid()))
        self.assertFalse(image_jobs._pid_alive(os.getpid()))
        self.assertIn("HT-Q1", job_ledger.drop_queued(image_jobs._pid_alive))

    def _run_report(self, recs):
        for rec in recs:
            job_ledger.add(rec)
        with mock.patch.object(image_jobs, "_send_text") as m_text:
            n = image_jobs._report_queued_lost()
        return n, m_text

    def test_one_lost_job_gets_one_sentence(self):
        n, m_text = self._run_report([self._queued(pid=999999)])
        self.assertEqual(1, n)
        m_text.assert_called_once()
        self.assertIn("还没开始画", m_text.call_args[0][2])
        self.assertIn("silver", m_text.call_args[0][2])

    def test_batch_of_queued_jobs_is_summed_not_spammed(self):
        """一次批量 5 张 → **一句**，不是 5 句（2026-10-18 刷屏的教训）。"""
        recs = [self._queued(tag="HT-Q%d" % i, pid=999999) for i in range(5)]
        n, m_text = self._run_report(recs)
        self.assertEqual(5, n)
        m_text.assert_called_once()
        text = m_text.call_args[0][2]
        self.assertIn("5 张", text)
        self.assertEqual("private", m_text.call_args[0][0])   # 发回原会话

    def test_web_side_has_nowhere_to_send(self):
        # 跑图台/漫画台 target 是 None：没有「回一句」的去处，静默清掉即可
        n, m_text = self._run_report(
            [self._queued(pid=999999, target=None, target_id=None)])
        self.assertEqual(1, n)
        m_text.assert_not_called()

    def test_report_is_wired_into_resume_pending(self):
        """重启入口必须真的调到它——这条是整个改动的落地点。"""
        job_ledger.add(self._queued(pid=999999))
        with mock.patch.object(image_jobs, "_resumed", False), \
             mock.patch.object(image_jobs, "_pid_alive", return_value=False):
            t = image_jobs.resume_pending()
            if t is not None:
                t.join(5)
        self.assertEqual({}, job_ledger.load())


class EnqueueRecordsQueuedTest(_TempLedger):
    """`enqueue` 一进队就记账（不是等提交）。"""

    def setUp(self):
        super().setUp()
        self._saved = [mock.patch.object(image_jobs, "_ensure_worker",
                                         lambda: None),
                       mock.patch.object(image_jobs.comfy_gate, "add_pending",
                                         lambda n=1: 0)]
        for p in self._saved:
            p.start()

    def tearDown(self):
        for p in self._saved:
            p.stop()
        image_jobs._COMFY.queue.clear()
        image_jobs._COMFY.per_session.clear()
        super().tearDown()

    def test_enqueue_writes_a_queued_record(self):
        job, reason = image_jobs.enqueue("private", "42", {"1": {}},
                                         skill="silver", prompt="1girl")
        self.assertIsNone(reason)
        rec = job_ledger.load()[job.tag]
        self.assertEqual("queued", rec["state"])
        self.assertEqual(os.getpid(), rec["pid"])
        self.assertEqual("42", rec["target_id"])
        # 只记轻量字段：workflow 不进账本（那才是持久化队列的难点）
        self.assertNotIn("workflow", rec)

    def test_quiet_is_not_recorded(self):
        job, _ = image_jobs.enqueue("private", "42", {"1": {}},
                                    skill="silver", quiet=True)
        self.assertNotIn(job.tag, job_ledger.load())

    def test_finish_of_a_never_submitted_job_clears_it(self):
        """排队中被取消/失败（压根没提交）也要划账，否则下次启动会误报。"""
        job, _ = image_jobs.enqueue("private", "42", {"1": {}}, skill="silver")
        self.assertIn(job.tag, job_ledger.load())
        image_jobs._finish(job)
        self.assertNotIn(job.tag, job_ledger.load())


if __name__ == "__main__":
    unittest.main()
