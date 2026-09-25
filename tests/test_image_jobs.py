"""生图后台投递 + generate_image 的 QQ 异步分流测试。

原则：零网络、零显卡、零真实线程等待。ComfyUI 的 HTTP、QQ 发送端、线程
本身全 mock，只测判定逻辑（排队上限 / 成功发回 / 失败说一句 / 同步异步
分流）。
"""

import unittest
from unittest import mock

from app import image_jobs
from app import qq_api
from app.tools.normal import generate_image


class _SyncThread:
    """替身线程：start() 直接同步跑完，免得测试里真去等后台。"""

    def __init__(self, target=None, args=(), **kwargs):
        self.target = target
        self.args = args

    def start(self):
        if self.target:
            self.target(*self.args)


class _Base(unittest.TestCase):
    def setUp(self):
        image_jobs._inflight.clear()
        self.sent_images = []
        self.sent_texts = []

        def _fake_image(target, tid, url, caption=""):
            self.sent_images.append((target, tid, url))

        def _fake_text(target, tid, text):
            self.sent_texts.append((target, tid, text))

        p = mock.patch.object(image_jobs, "threading",
                              mock.Mock(Thread=_SyncThread))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(image_jobs, "_send_image", _fake_image)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(image_jobs, "_send_text", _fake_text)
        p.start()
        self.addCleanup(p.stop)


class PollTest(_Base):
    """轮询与取图：网络异常一律当「还没好」。"""

    def _patch_get(self, payload=None, error=None):
        """只替换 image_jobs 眼里那个 requests 模块，不动全局 requests。"""
        def _get(url, timeout=None):
            if error:
                raise error
            return mock.Mock(json=lambda: payload,
                             raise_for_status=lambda: None)
        p = mock.patch.object(image_jobs, "requests", mock.Mock(get=_get))
        p.start()
        self.addCleanup(p.stop)

    def test_returns_entry_when_prompt_finished(self):
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        self._patch_get(payload={"pid": entry})
        self.assertEqual(image_jobs.poll_once("pid"), entry)

    def test_returns_none_before_finished(self):
        self._patch_get(payload={})
        self.assertIsNone(image_jobs.poll_once("pid"))

    def test_network_error_is_treated_as_not_ready(self):
        self._patch_get(error=OSError("boom"))
        self.assertIsNone(image_jobs.poll_once("pid"))

    def test_output_images_flattens_nodes(self):
        entry = {"outputs": {"1": {"images": [{"filename": "a.png"}]},
                             "2": {"images": [{"filename": "b.png"}]}}}
        self.assertEqual(image_jobs.output_images(entry), ["a.png", "b.png"])

    def test_output_images_empty_when_no_image(self):
        self.assertEqual(image_jobs.output_images({"outputs": {}}), [])


class WaitTest(_Base):
    """wait_done：出图就返回，一直不出就超时抛错。"""

    def test_returns_entry_once_ready(self):
        entry = {"outputs": {}}
        with mock.patch.object(image_jobs, "POLL_INTERVAL", 0):
            with mock.patch.object(image_jobs, "poll_once",
                                   side_effect=[None, None, entry]):
                self.assertEqual(image_jobs.wait_done("pid"), entry)

    def test_timeout_raises(self):
        with mock.patch.object(image_jobs, "POLL_INTERVAL", 0):
            with mock.patch.object(image_jobs, "poll_once", return_value=None):
                with self.assertRaises(TimeoutError):
                    # 给一个正的极小值：0 会被 wait_done 当成「没传」回退默认
                    image_jobs.wait_done("pid", timeout=0.001)


class SubmitTest(_Base):
    """排队上限与名额释放。

    默认把 _run 挡掉：这一组只关心「接不接、放不放」，真去等出图会打到
    真实的 ComfyUI 上。
    """

    def setUp(self):
        super().setUp()
        p = mock.patch.object(image_jobs, "_run")
        p.start()
        self.addCleanup(p.stop)

    def _submit(self, pid="p1"):
        return image_jobs.submit("group", "9", pid)

    def test_two_jobs_accepted_third_rejected(self):
        ok1, n1 = self._submit("p1")
        ok2, _ = self._submit("p2")
        ok3, n3 = self._submit("p3")
        self.assertEqual((ok1, n1), (True, 1))
        self.assertTrue(ok2)
        self.assertFalse(ok3)
        self.assertEqual(n3, image_jobs.MAX_INFLIGHT)

    def test_slots_are_per_conversation(self):
        self.assertTrue(self._submit("p1")[0])
        self.assertTrue(self._submit("p2")[0])
        self.assertTrue(image_jobs.submit("group", "8", "p3")[0])

    def test_failed_thread_start_does_not_hold_slot(self):
        p = mock.patch.object(image_jobs, "threading",
                              mock.Mock(Thread=mock.Mock(
                                  side_effect=RuntimeError("no thread"))))
        p.start()
        self.addCleanup(p.stop)
        with self.assertRaises(RuntimeError):
            self._submit("p1")
        self.assertEqual(image_jobs.inflight_count("group", "9"), 0)


class DeliverTest(_Base):
    """出图后发回原会话；失败在群里说一句。"""

    def _entry(self, names=("a.png",)):
        return {"outputs": {"9": {"images": [{"filename": n} for n in names]}}}

    def test_success_sends_image_back_to_same_conversation(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            image_jobs.submit("group", "9", "p1")
        self.assertEqual(len(self.sent_images), 1)
        target, tid, url = self.sent_images[0]
        self.assertEqual((target, tid), ("group", "9"))
        self.assertIn("a.png", url)
        self.assertEqual(self.sent_texts, [])   # 只发图，不说话

    def test_private_target_kept(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            image_jobs.submit("private", "123", "p1")
        self.assertEqual(self.sent_images[0][:2], ("private", "123"))

    def test_slot_released_after_job_finishes(self):
        """任务跑完（哪怕失败）名额就要还回去，否则这个会话再也画不了。"""
        with mock.patch.object(image_jobs, "wait_done",
                               return_value={"outputs": {}}):
            image_jobs.submit("group", "9", "p1")
            self.assertEqual(image_jobs.inflight_count("group", "9"), 0)
            self.assertTrue(image_jobs.submit("group", "9", "p2")[0])

    def test_failure_tells_the_group(self):
        with mock.patch.object(image_jobs, "wait_done",
                               side_effect=TimeoutError("生成超时 (3600s)")):
            image_jobs.submit("group", "9", "p1")
        self.assertEqual(self.sent_images, [])
        self.assertEqual(len(self.sent_texts), 1)
        self.assertEqual(self.sent_texts[0][:2], ("group", "9"))
        self.assertIn("图没画出来", self.sent_texts[0][2])
        self.assertIn("超时", self.sent_texts[0][2])

    def test_empty_output_counts_as_failure(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value={"outputs": {}}):
            image_jobs.submit("group", "9", "p1")
        self.assertEqual(self.sent_images, [])
        self.assertTrue(self.sent_texts)

    def test_send_failure_falls_back_to_notice(self):
        """图发出去失败也算失败：照样说一句，不让异常冒出后台线程。"""
        p = mock.patch.object(image_jobs, "_send_image",
                              side_effect=OSError("down"))
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            image_jobs.submit("group", "9", "p1")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("图没画出来", self.sent_texts[0][2])


class GenerateImageSplitTest(unittest.TestCase):
    """generate_image：QQ 侧提交即返回，网页侧照旧同步等。"""

    def setUp(self):
        image_jobs._inflight.clear()
        p = mock.patch.object(generate_image, "load_skill",
                              return_value={"workflow": {"1": {}},
                                            "character": ""})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(generate_image, "_queue_prompt",
                              return_value="pid")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(generate_image, "_qq_gate", return_value=None)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(generate_image, "is_cancelled",
                              return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.wait = mock.Mock(return_value={
            "outputs": {"9": {"images": [{"filename": "a.png"}]}}})
        p = mock.patch.object(generate_image, "_wait_for_completion",
                              self.wait)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(image_jobs, "threading",
                              mock.Mock(Thread=_SyncThread))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(image_jobs, "_send_image")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(image_jobs, "_send_text")
        p.start()
        self.addCleanup(p.stop)

    def _call(self, ctx):
        with mock.patch.object(qq_api, "current_context", return_value=ctx):
            with mock.patch.object(image_jobs, "wait_done",
                                   return_value={"outputs": {}}):
                return generate_image.tool["function"]("a cat")

    def test_qq_returns_immediately_without_waiting(self):
        out = self._call(("group", "9"))
        self.assertIn("已经在画了", out)
        self.wait.assert_not_called()          # 没在这儿等显卡
        self.assertEqual(image_jobs.inflight_count("group", "9"), 0)

    def test_web_still_waits_and_returns_url(self):
        out = self._call((None, None))
        self.wait.assert_called_once()
        self.assertIn("/api/image/a.png", out)

    def test_qq_queue_full_tells_model_to_drop_it(self):
        with mock.patch.object(image_jobs, "_run"):
            self._call(("group", "9"))
            self._call(("group", "9"))
        out = self._call(("group", "9"))
        self.assertIn("排着", out)
        self.assertIn(str(image_jobs.MAX_INFLIGHT), out)


if __name__ == "__main__":
    unittest.main()
