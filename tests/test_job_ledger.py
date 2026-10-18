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

from app import image_jobs, image_log, job_ledger


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


if __name__ == "__main__":
    unittest.main()
