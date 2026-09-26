"""生图队列 + generate_image 的排队/异步分流测试。

原则：零网络、零显卡、零真实线程等待。ComfyUI 的 HTTP、QQ 发送端、worker
线程全 mock，只测判定逻辑（谁排队、谁被拒、超时怎么收场、失败怎么回话）。
"""

import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
from app import image_jobs
from app import qq_api
from app.config import QQ_AGENT_ID
from app.tools.normal import generate_image


class _Base(unittest.TestCase):
    """统一把 worker 线程挡在门外：任务入队后由测试自己 _drain() 驱动。

    真起线程的话，断言就得跟后台线程抢时序；_ensure_worker 一成空操作，队列
    就变成测试手里的确定性对象。
    """

    def setUp(self):
        image_jobs._reset()
        self.sent_images = []
        self.sent_texts = []

        def _fake_image(target, tid, url, caption=""):
            self.sent_images.append((target, tid, url))

        def _fake_text(target, tid, text):
            self.sent_texts.append((target, tid, text))

        for target, repl in (("_ensure_worker", lambda: None),
                             ("_queue_prompt", lambda wf: "pid"),
                             ("_send_image", _fake_image),
                             ("_send_text", _fake_text),
                             # 超时路径会真去轮询 ComfyUI 的 /queue，测试里挡掉
                             ("_wait_comfy_idle", lambda timeout=90: True)):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _enqueue(self, ctx=("group", "9"), wf=None):
        return image_jobs.enqueue(ctx[0], ctx[1], wf or {"1": {}})


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


class IdleWaitTest(unittest.TestCase):
    """超时中断后的事件驱动等待：running 清空才放行，ComfyUI 挂了也不卡队。

    故意不继承 _Base——那里把 _wait_comfy_idle 本身 mock 成了空操作，
    继承它就没东西可测了。
    """

    def setUp(self):
        image_jobs._reset()

    def _mock_get(self, payloads):
        """GET /queue 依次返回 payloads 里的每一份；用尽后停在最后一份。"""
        seq = iter(payloads)

        def _get(url, timeout=None):
            try:
                payload = next(seq)
            except StopIteration:
                payload = payloads[-1]
            return mock.Mock(json=lambda: payload,
                             raise_for_status=lambda: None)

        return mock.patch.object(image_jobs, "requests",
                                 mock.Mock(get=_get, post=mock.Mock()))

    def test_returns_true_once_running_empty(self):
        posts = []
        with mock.patch.object(image_jobs, "POLL_INTERVAL", 0), \
                self._mock_get([{"queue_running": [], "queue_pending": []}]) \
                as reqs:
            reqs.post.side_effect = \
                lambda url, **kw: posts.append((url, kw.get("json")))
            self.assertTrue(image_jobs._wait_comfy_idle(timeout=5))
        # 空了之后要补一次 /free，把模型缓存也清掉
        self.assertTrue(any(u.endswith("/free") for u, _ in posts))

    def test_waits_until_running_clears(self):
        """旧任务还在跑就继续等，退场了才返回 True。"""
        with mock.patch.object(image_jobs, "POLL_INTERVAL", 0), \
                self._mock_get([{"queue_running": [{"x": 1}]},
                                {"queue_running": [{"x": 1}]},
                                {"queue_running": []}]):
            self.assertTrue(image_jobs._wait_comfy_idle(timeout=30))

    def test_gives_up_after_timeout(self):
        """ComfyUI 一直不空（比如挂了）——放弃等待，不能把队列卡死。"""
        with mock.patch.object(image_jobs, "POLL_INTERVAL", 0), \
                self._mock_get([{"queue_running": [{"x": 1}]}]):
            self.assertFalse(image_jobs._wait_comfy_idle(timeout=0.05))

    def test_network_error_counts_as_not_idle(self):
        """查不到队列状态时当「还没空」继续等，等满时限才放弃。"""
        with mock.patch.object(image_jobs, "POLL_INTERVAL", 0), \
                mock.patch.object(image_jobs, "requests",
                                  mock.Mock(get=mock.Mock(
                                      side_effect=OSError("down")))):
            self.assertFalse(image_jobs._wait_comfy_idle(timeout=0.05))


class QueueTest(_Base):
    """谁排队、谁被拒。全局就一条队，多会话一起排。"""

    def test_jobs_line_up_across_conversations(self):
        """不同会话的任务进的是同一条队——这就是「全局串行」的意思。"""
        a, _ = self._enqueue(("group", "9"))
        b, _ = self._enqueue(("group", "8"))
        c, _ = self._enqueue(("private", "123"))
        self.assertEqual(list(image_jobs._queue), [a, b, c])
        self.assertEqual(image_jobs.queue_depth(), 3)

    def test_ahead_counts_the_running_one(self):
        a, _ = self._enqueue()
        b, _ = self._enqueue()
        self.assertEqual(image_jobs.ahead_of(a), 0)
        self.assertEqual(image_jobs.ahead_of(b), 1)
        image_jobs._take_nowait()          # 相当于 worker 开始跑 a
        self.assertEqual(image_jobs.ahead_of(b), 1)   # a 还在跑，仍在前头

    def test_per_session_limit(self):
        for _ in range(image_jobs.MAX_INFLIGHT):
            job, reason = self._enqueue()
            self.assertIsNone(reason)
            self.assertIsNotNone(job)
        job, reason = self._enqueue()
        self.assertIsNone(job)
        self.assertIn("排着", reason)

    def test_global_queue_limit_rejects_anyone(self):
        """全局队排满就拒——不管是谁的会话。这是「别一次塞太多」的总闸。"""
        for i in range(image_jobs.MAX_QUEUE):
            # 每次换会话，绕开每会话上限，专门顶全局这条
            job, reason = self._enqueue(("group", str(i)))
            self.assertIsNone(reason)
        job, reason = self._enqueue(("group", "999"))
        self.assertIsNone(job)
        self.assertIn("太多", reason)

    def test_slot_released_after_finish(self):
        job, _ = self._enqueue()
        self.assertEqual(image_jobs.inflight_count("group", "9"), 1)
        image_jobs._take_nowait()
        image_jobs._finish(job)
        self.assertEqual(image_jobs.inflight_count("group", "9"), 0)
        self.assertIsNone(self._enqueue()[1])


class ProcessTest(_Base):
    """跑任务：出图发回原会话、失败说一句、超时中断并清干净。"""

    def _entry(self, names=("a.png",)):
        return {"outputs": {"9": {"images": [{"filename": n} for n in names]}}}

    def test_success_sends_image_back(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue()
            image_jobs._drain()
        self.assertEqual(len(self.sent_images), 1)
        self.assertEqual(self.sent_images[0][:2], ("group", "9"))
        self.assertIn("a.png", self.sent_images[0][2])
        self.assertEqual(self.sent_texts, [])       # 只发图，不说话
        self.assertEqual(image_jobs.queue_depth(), 0)

    def test_private_target_kept(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue(("private", "123"))
            image_jobs._drain()
        self.assertEqual(self.sent_images[0][:2], ("private", "123"))

    def test_timeout_interrupts_and_cleans_comfyui(self):
        """超时不是「不等了」——要真把它从 ComfyUI 里摘掉并释放显存。

        从前任务已经在 ComfyUI 手里，agent 侧只能放弃等待，僵尸继续占着显存
        和队列。改成全局串行之后才敢用 /interrupt：那一刻正在跑的就是我们
        这一张。
        """
        posts = []
        with mock.patch.object(image_jobs, "wait_done",
                               side_effect=TimeoutError("超时")), \
                mock.patch.object(image_jobs, "requests",
                                  mock.Mock(post=lambda url, **kw:
                                            posts.append((url, kw.get("json"))))), \
                mock.patch.object(image_jobs, "_wait_comfy_idle",
                                  return_value=True) as idle:
            self._enqueue()
            image_jobs._drain()
        urls = [u for u, _ in posts]
        self.assertTrue(any(u.endswith("/interrupt") for u in urls))
        self.assertTrue(any(u.endswith("/free") for u in urls))
        # 队列里那一份也要按 prompt_id 删掉
        self.assertIn({"delete": ["pid"]},
                      [p for u, p in posts if u.endswith("/queue")])
        # 中断后要等 ComfyUI 真正退场（queue_running 清空）才放行下一个任务
        idle.assert_called_once()

    def test_timeout_tells_group_to_redo(self):
        with mock.patch.object(image_jobs, "wait_done",
                               side_effect=TimeoutError("超时")), \
                mock.patch.object(image_jobs, "requests",
                                  mock.Mock(post=lambda *a, **k: None)):
            self._enqueue()
            image_jobs._drain()
        self.assertEqual(self.sent_images, [])
        self.assertEqual(len(self.sent_texts), 1)
        self.assertEqual(self.sent_texts[0][:2], ("group", "9"))
        self.assertIn("超时", self.sent_texts[0][2])
        self.assertIn("重新生成", self.sent_texts[0][2])

    def test_empty_output_counts_as_failure(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value={"outputs": {}}), \
                mock.patch.object(image_jobs, "requests",
                                  mock.Mock(post=lambda *a, **k: None)):
            self._enqueue()
            image_jobs._drain()
        self.assertEqual(self.sent_images, [])
        self.assertTrue(self.sent_texts)

    def test_send_failure_falls_back_to_notice(self):
        """图发出去失败也算失败：照样说一句，不让异常冒出 worker 线程。"""
        p = mock.patch.object(image_jobs, "_send_image",
                              side_effect=OSError("down"))
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue()
            image_jobs._drain()
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("图没画出来", self.sent_texts[0][2])

    def test_queue_keeps_running_after_a_failure(self):
        """前一张失败不该把队卡住——后面的人照跑。"""
        with mock.patch.object(
                image_jobs, "wait_done",
                side_effect=[TimeoutError("超时"), self._entry()]), \
                mock.patch.object(image_jobs, "requests",
                                  mock.Mock(post=lambda *a, **k: None)):
            self._enqueue()
            self._enqueue()
            image_jobs._drain()
        self.assertEqual(len(self.sent_images), 1)   # 第二张发出来了
        self.assertEqual(image_jobs.queue_depth(), 0)

    def test_web_job_keeps_entry_and_stays_silent(self):
        """网页侧（target=None）不往任何会话发消息，结果留给 job.entry。"""
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            job, _ = self._enqueue((None, None))
            image_jobs._drain()
        self.assertIsNotNone(job.entry)
        self.assertEqual(self.sent_images, [])
        self.assertEqual(self.sent_texts, [])


class SendImageTest(unittest.TestCase):
    """后台投递那条发图出口同样要过 image_out —— 两条路都不能把工作流带出去。"""

    def setUp(self):
        # _send_image 现在要读发图格式（load_settings），必须把 AGENTS_DIR 拨到
        # 临时目录：否则读的是真的 agents/qq/settings.json，结果跟着本机配置跑。
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(agents, "AGENTS_DIR",
                              os.path.join(self.tmp.name, "agents"))
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)

    def _capture(self, fmts, sent):
        from app import image_out
        return mock.patch.object(
            image_out, "prepare_for_send",
            lambda f, fmt="jpg": (fmts.append(fmt), "C:/tmp/y." + fmt)[1]), \
            mock.patch.object(qq_api, "send_image",
                              lambda target, tid, path, caption="":
                              sent.append((target, tid, path)))

    def test_goes_through_image_out(self):
        sent, fmts = [], []
        p1, p2 = self._capture(fmts, sent)
        with p1, p2:
            image_jobs._send_image("group", "9", "b.png")
        self.assertEqual(sent, [("group", "9", "C:/tmp/y.jpg")])
        self.assertEqual(fmts, ["jpg"])          # 没配过 = jpg

    def test_group_override_reaches_send_path(self):
        """单群设成 png 时，后台投递这条出口也得跟着走 png。"""
        agents.save_settings(QQ_AGENT_ID, {"image_send_format": "jpg",
                                           "image_send_format_overrides":
                                               {"9": "png"}})
        sent, fmts = [], []
        p1, p2 = self._capture(fmts, sent)
        with p1, p2:
            image_jobs._send_image("group", "9", "b.png")
        self.assertEqual(fmts, ["png"])

    def test_falls_back_to_comfy_url(self):
        """转换拉不到图时回落原图 URL，图照样发得出去。"""
        from app import image_out

        sent = []
        with mock.patch.object(image_out._session, "get",
                               mock.Mock(side_effect=OSError("comfy 不可达"))), \
             mock.patch.object(qq_api, "send_image",
                               lambda target, tid, path, caption="":
                               sent.append(path)):
            image_jobs._send_image("group", "9", "b c.png")
        self.assertIn("/view?filename=b%20c.png", sent[0])


class GenerateImageSplitTest(unittest.TestCase):
    """generate_image：QQ 侧排队即返回，网页侧同步等出图。"""

    def setUp(self):
        image_jobs._reset()
        for target, repl in (
            ("load_skill", mock.Mock(return_value={
                "workflow": {"1": {}}, "character": ""})),
            ("_qq_gate", mock.Mock(return_value=None)),
            ("is_cancelled", mock.Mock(return_value=False)),
        ):
            p = mock.patch.object(generate_image, target, repl)
            p.start()
            self.addCleanup(p.stop)
        for target, repl in (
            ("_queue_prompt", lambda wf: "pid"),
            ("_ensure_worker", lambda: None),
            ("_send_image", mock.Mock()),
            ("_send_text", mock.Mock()),
        ):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)

        def _sync_wait(self, poll=2):
            """测试里没有 worker 线程，wait 时自己把队列同步跑完。

            真等的话 job.done 永远不会 set（没人处理它），用例会挂死。
            """
            image_jobs._drain()
            if self.error is not None:
                raise self.error
            return self.entry

        p = mock.patch.object(image_jobs.Job, "wait", _sync_wait)
        p.start()
        self.addCleanup(p.stop)

    def _entry(self):
        return {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}

    def _call(self, ctx, error=None):
        kw = ({"side_effect": error} if error is not None
              else {"return_value": self._entry()})
        with mock.patch.object(qq_api, "current_context", return_value=ctx), \
                mock.patch.object(image_jobs, "wait_done", **kw):
            out = generate_image.tool["function"]("a cat")
            image_jobs._drain()
            return out

    def test_qq_returns_immediately_without_waiting(self):
        out = self._call(("group", "9"))
        self.assertIn("已经在画了", out)
        self.assertEqual(image_jobs.inflight_count("group", "9"), 0)

    def test_qq_says_how_many_are_ahead(self):
        """排队时要告诉模型前面还有几张，好让它跟对方交代一句。"""
        with mock.patch.object(qq_api, "current_context",
                               return_value=("group", "9")), \
                mock.patch.object(image_jobs, "wait_done",
                                  return_value={"outputs": {}}):
            generate_image.tool["function"]("a cat")        # 先占住队首
            out = generate_image.tool["function"]("a cat")  # 这一张排在后面
        self.assertIn("前面还有 1 张", out)

    def test_web_still_waits_and_returns_url(self):
        out = self._call((None, None))
        self.assertIn("/api/image/a.png", out)

    def test_web_reports_timeout(self):
        out = self._call((None, None), error=TimeoutError(
            "生成超时 (%ds)" % image_jobs.TASK_TIMEOUT))
        self.assertIn("超时", out)

    def test_qq_queue_full_does_not_reach_comfyui(self):
        """被拒时一步都不该碰 ComfyUI——否则就是没人发的孤儿图。

        从前是先 _queue_prompt 再查名额：被拒那一次图照样会画出来，可没有
        任何线程登记它，于是永远发不出去——群里看到的就是「图生成了但不发
        群」，模型还被告知「当没画过」。
        """
        with mock.patch.object(qq_api, "current_context",
                               return_value=("group", "9")), \
                mock.patch.object(image_jobs, "wait_done",
                                  return_value={"outputs": {}}), \
                mock.patch.object(image_jobs, "_queue_prompt",
                                  return_value="pid") as qp:
            for _ in range(image_jobs.MAX_INFLIGHT):
                generate_image.tool["function"]("a cat")
            out = generate_image.tool["function"]("a cat")   # 被拒
            image_jobs._drain()
        self.assertIn("排着", out)
        # 只有真正入了队的那几张被送进 ComfyUI
        self.assertEqual(qp.call_count, image_jobs.MAX_INFLIGHT)


if __name__ == "__main__":
    unittest.main()
