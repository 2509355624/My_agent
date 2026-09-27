"""生图队列 + generate_image 的排队/异步分流测试。

原则：零网络、零显卡、零真实线程等待。ComfyUI 的 HTTP、QQ 发送端、worker
线程全 mock，只测判定逻辑（谁排队、谁被拒、超时怎么收场、失败怎么回话）。
"""

import os
import tempfile
import threading
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
        # 中断必须是定向的：只杀自己这张，不能全局劈到别的 worker 正在跑的图
        self.assertIn({"prompt_id": "pid"},
                      [p for u, p in posts if u.endswith("/interrupt")])
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
            # 入队前探活：默认「ComfyUI 在」，需要测拒收的用例自己再 patch 掉
            ("comfy_alive", mock.Mock(return_value=True)),
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

    def test_offline_comfyui_refused_before_queueing(self):
        """ComfyUI 挂了就当场拒掉——接了单模型就会说「排上了」，几十秒后再打脸。

        队列在 agent 侧，enqueue 从来不碰 ComfyUI，所以没有这道探活时它一定
        会「成功」：模型拿到「已经排上队了」去跟对方承诺，worker 才在 /prompt
        上撞到连接失败。群里先看到承诺、再看到「图没画出来」。
        """
        with mock.patch.object(qq_api, "current_context",
                               return_value=("group", "9")), \
                mock.patch.object(image_jobs, "comfy_alive",
                                  return_value=False), \
                mock.patch.object(image_jobs, "_queue_prompt") as qp:
            out = generate_image.tool["function"]("a cat")
        self.assertIn("没在线", out)
        self.assertNotIn("排上", out)
        self.assertEqual(image_jobs.queue_depth(), 0)   # 一张都没进队
        self.assertFalse(qp.called)                     # 更没碰 ComfyUI


class ComfyAliveTest(unittest.TestCase):
    """入队前探活：探不到就别收单，别让模型先承诺再被打脸。"""

    def _patch_get(self, error=None, raise_on_status=None):
        """只替换 image_jobs 眼里那个 requests，不动全局 requests。"""
        def _get(url, timeout=None):
            if error:
                raise error
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock(side_effect=raise_on_status)
            return resp
        p = mock.patch.object(image_jobs, "requests", mock.Mock(get=_get))
        p.start()
        self.addCleanup(p.stop)

    def test_true_when_system_stats_answers(self):
        self._patch_get()
        self.assertTrue(image_jobs.comfy_alive())

    def test_false_when_connection_refused(self):
        """8188 没在听 = 这次事故的现场，必须探得出来。"""
        self._patch_get(error=OSError("Connection refused"))
        self.assertFalse(image_jobs.comfy_alive())

    def test_false_when_comfyui_answers_an_error(self):
        self._patch_get(raise_on_status=OSError("500 Server Error"))
        self.assertFalse(image_jobs.comfy_alive())


class FailTextTest(unittest.TestCase):
    """失败话术：连接类错误说人话，别把 HTTPConnectionPool 半截乱码丢进群。"""

    _RAW = ("HTTPConnectionPool(host='127.0.0.1', port=8188): "
            "Max retries exceeded with url: /prompt")

    def test_unreachable_says_offline(self):
        text = image_jobs._fail_text(OSError(self._RAW))
        self.assertIn("没在线", text)
        self.assertNotIn("HTTPConnectionPool", text)

    def test_timeout_keeps_its_own_wording(self):
        text = image_jobs._fail_text(TimeoutError("生成超时 (600s)"))
        self.assertIn("超时", text)
        self.assertIn("重新生成", text)

    def test_send_stage_does_not_blame_comfyui(self):
        """投递阶段失败跟 ComfyUI 在不在无关，别往它头上安。"""
        text = image_jobs._fail_text(OSError("down"), stage="send")
        self.assertIn("图没画出来", text)
        self.assertNotIn("没在线", text)

    def test_http_error_is_not_reported_as_offline(self):
        """ComfyUI 活着、只是把工作流拒了——那不是「没在线」。

        判据是「异常带不带 response」：requests 只有真拿到回应才会挂上
        response，ConnectionError 那个字段恒为 None。
        """
        import requests
        resp = requests.Response()
        resp.status_code = 400
        exc = requests.exceptions.HTTPError("400 Bad Request", response=resp)
        self.assertFalse(image_jobs._is_unreachable(exc))
        self.assertNotIn("没在线", image_jobs._fail_text(exc))


class ChannelSwitchTest(_Base):
    """换渠道先 /free：同渠道连画保持模型热，跨渠道才释放。

    背景（2026-09-27 实测）：12GB 显存 + 16GB 内存撑不住两个渠道的模型同时
    驻留。anima 单阶段连跑两张都正常，紧接着同一个 ComfyUI 会话里跑 qwen
    （文本编码器 6GB + unet 4.5GB），采样到一半就 TDR，ComfyUI 变成僵尸。
    所以 skill 一变就先 /free 把上一个渠道的模型卸掉。
    """

    def setUp(self):
        super().setUp()
        self.events = []

        def _free():
            self.events.append("free")

        def _submit(wf):
            self.events.append("submit")
            return "pid"

        for target, repl in (("_report_and_free", _free),
                             ("_queue_prompt", _submit)):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, skill=None):
        """入队一张并同步跑完。wait_done 必须挡掉，否则会真去轮询 ComfyUI。"""
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        with mock.patch.object(image_jobs, "wait_done", return_value=entry):
            image_jobs.enqueue("group", "9", {"1": {}}, skill)
            image_jobs._drain()

    def test_first_job_does_not_free(self):
        """第一次没有「上一个渠道」，没什么可卸的。"""
        self._run(skill="anima")
        self.assertEqual(self.events, ["submit"])

    def test_same_skill_twice_never_frees(self):
        """同渠道连画必须保持模型热的——否则每次都白等一次重新加载。"""
        self._run(skill="anima")
        self._run(skill="anima")
        self.assertEqual(self.events, ["submit", "submit"])

    def test_switch_frees_before_submitting(self):
        """顺序要紧：先 free 再 submit，否则新任务还是和旧模型抢显存。"""
        self._run(skill="anima")
        self.events.clear()
        self._run(skill="qwen_image_v1")
        self.assertEqual(self.events, ["free", "submit"])

    def test_switching_back_also_frees(self):
        """来回切也算切换，两个方向都要释放。"""
        self._run(skill="anima")
        self._run(skill="qwen_image_v1")
        self.events.clear()
        self._run(skill="anima")
        self.assertEqual(self.events, ["free", "submit"])

    def test_no_skill_keeps_old_behaviour(self):
        """老调用方不传 skill：绝不能因此多打 /free，行为要和从前一样。"""
        self._run()
        self._run()
        self.assertEqual(self.events, ["submit", "submit"])

    def test_skill_none_after_real_skill_is_not_a_switch(self):
        """传 None 不是「换渠道」——不能拿 None 去和 anima 比出一次切换。"""
        self._run(skill="anima")
        self.events.clear()
        self._run()
        self.assertEqual(self.events, ["submit"])


class _FakeTime:
    """替掉 image_jobs 眼里的 time。

    只换模块属性，不动 stdlib 的 time——后者会影响整个进程（含 mock 自己）。
    clock 默认是**定值**（防抖判定用）；要跑 _restart_comfy 的轮询循环时传
    一个会走的时钟进去，否则 `while time.time() - start < timeout` 永远成立。
    """

    def __init__(self, clock=None):
        self._clock = clock or (lambda: 1000.0)
        self.slept = []

    def time(self):
        return self._clock()

    def sleep(self, seconds):
        self.slept.append(seconds)


class _TickingClock:
    """每读一次往前走 step 秒——把轮询循环快速推到超时。"""

    def __init__(self, start=1000.0, step=10.0):
        self.t = start
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


class RestartOnLowRamTest(unittest.TestCase):
    """可用内存过低就重启 ComfyUI（走 Manager 的 /manager/reboot）。

    为什么必须是「重启」而不是「/free」：/free 只做
    model.to(offload_device)——把权重从显存搬到 CPU、**不删**，所以进程
    RSS 一个字节都不降（2026-09-27 实测 8025 → 8025 MB）。而 ComfyUI 的
    常驻内存每张图涨约 600MB、只涨不落。16GB 物理内存被挤干之后的症状是
    「卡」不是「崩」：GGUF 每次从磁盘重读（5.5s → 68s）、采样卡在 0/N 一百
    秒，最后被 IMAGE_GEN_TIMEOUT 掐掉。
    """

    def setUp(self):
        image_jobs._reset()
        self.stat_calls = []
        self.reboot_calls = []

    def _patch(self, stats, reboot_status=200, clock=None, reboot_error=None):
        """装一个假的 requests + 假的 time。

        stats 是 /system_stats 的应答序列：数字 = 那一刻可用多少 GB，异常
        实例 = 那一刻连不上。序列用完就重复最后一项。
        reboot_error 用来模拟真实的重启应答——ComfyUI 是先 exit 再回包的，
        所以真机上拿到的是连接重置而不是 200。
        """
        seq = list(stats)
        box = {"i": 0}

        def _next_stats():
            idx = min(box["i"], len(seq) - 1)
            box["i"] += 1
            item = seq[idx]
            if isinstance(item, Exception):
                raise item
            resp = mock.Mock()
            resp.status_code = 200
            resp.raise_for_status = mock.Mock()
            resp.json = mock.Mock(
                return_value={"system": {"ram_free": item * 2 ** 30}})
            return resp

        def _get(url, timeout=None):
            if url.endswith("/system_stats"):
                self.stat_calls.append(url)
                return _next_stats()
            if url.endswith("/manager/reboot"):
                self.reboot_calls.append(url)
                if reboot_error is not None:
                    raise reboot_error
                resp = mock.Mock()
                resp.status_code = reboot_status
                return resp
            raise AssertionError("测试没预期的 URL：" + url)

        for name, repl in (("requests", mock.Mock(get=_get)),
                           ("time", _FakeTime(clock))):
            p = mock.patch.object(image_jobs, name, repl)
            p.start()
            self.addCleanup(p.stop)

    def _threshold(self, gb):
        p = mock.patch.object(image_jobs, "COMFY_MIN_FREE_RAM_GB", gb)
        p.start()
        self.addCleanup(p.stop)

    def test_above_threshold_does_not_restart(self):
        self._patch([5.0])
        image_jobs._maybe_restart_for_ram()
        self.assertEqual(self.reboot_calls, [])

    def test_below_threshold_restarts(self):
        """1.5GB = 本次事故的现场（实测 1.60GB 可用），必须重启。"""
        self._patch([1.5])
        image_jobs._maybe_restart_for_ram()
        self.assertEqual(len(self.reboot_calls), 1)

    def test_exactly_at_threshold_does_not_restart(self):
        """等于水位不算「低于」——边界不能来回抖。"""
        self._patch([3.0])
        image_jobs._maybe_restart_for_ram()
        self.assertEqual(self.reboot_calls, [])

    def test_zero_threshold_disables_the_feature(self):
        """0 = 关掉：连 /system_stats 都不该问。"""
        self._threshold(0)
        self._patch([0.5])
        image_jobs._maybe_restart_for_ram()
        self.assertEqual(self.reboot_calls, [])
        self.assertEqual(self.stat_calls, [])

    def test_unreachable_stats_does_not_restart(self):
        """问不到内存数就别动手——ComfyUI 可能根本没开。"""
        self._patch([OSError("Connection refused")])
        image_jobs._maybe_restart_for_ram()
        self.assertEqual(self.reboot_calls, [])

    def test_min_gap_blocks_a_second_restart(self):
        """连着两张都低于水位，只重启一次——不然就变成每张都重启。"""
        self._patch([1.5])
        image_jobs._maybe_restart_for_ram()
        image_jobs._maybe_restart_for_ram()
        self.assertEqual(len(self.reboot_calls), 1)

    def test_rejected_reboot_is_not_retried_on_every_image(self):
        """重启被拒（403）也算「试过了」。

        不记的话每张图都会再发一次请求、白等 3 秒、还刷一条警告。
        """
        self._patch([1.5], reboot_status=403)
        for _ in range(3):
            image_jobs._maybe_restart_for_ram()
        self.assertEqual(len(self.reboot_calls), 1)

    def test_restart_returns_true_when_comfyui_comes_back(self):
        """轮询到它回来就算成功——中间那几次连不上是正常的。"""
        self._patch([OSError("down"), OSError("down"), 6.0],
                    clock=_TickingClock())
        self.assertTrue(image_jobs._restart_comfy(timeout=120))

    def test_restart_returns_false_when_it_never_comes_back(self):
        """一直连不上就放弃返回 False，不能在这儿无限等。"""
        self._patch([OSError("down")], clock=_TickingClock())
        self.assertFalse(image_jobs._restart_comfy(timeout=30))

    def test_rejected_restart_returns_false_without_waiting(self):
        self._patch([5.0], reboot_status=403)
        self.assertFalse(image_jobs._restart_comfy(timeout=120))
        self.assertEqual(self.stat_calls, [])       # 没轮询，直接放弃

    def test_connection_reset_on_reboot_still_counts_as_restarting(self):
        """实测：ComfyUI 是**先 exit 再回包**的。

        客户端拿到的是 `ConnectionResetError`（10054），不是 200。这不能当
        失败——否则它真在重启时我们却判定失败、不等它回来，下一张图就撞上
        「ComfyUI 没在线」那句话，白等一场。
        """
        self._patch([OSError("down"), 6.0],
                    reboot_error=ConnectionResetError(10054, "reset"),
                    clock=_TickingClock())
        self.assertTrue(image_jobs._restart_comfy(timeout=120))

    def test_connection_reset_then_never_back_still_returns_false(self):
        """连接重置只说明「请求发出去了」，不代表一定会回来。"""
        self._patch([OSError("down")],
                    reboot_error=ConnectionResetError(10054, "reset"),
                    clock=_TickingClock())
        self.assertFalse(image_jobs._restart_comfy(timeout=30))

    def test_restart_clears_the_remembered_channel(self):
        """重启后 ComfyUI 里一个模型都没有了，别以为上个渠道还是热的。"""
        image_jobs._last_skill = "anima"
        self._patch([6.0], clock=_TickingClock())
        self.assertTrue(image_jobs._restart_comfy(timeout=120))
        self.assertIsNone(image_jobs._last_skill)

    def test_worker_checks_ram_after_every_job(self):
        """worker 必须在每张跑完之后看一眼内存。

        没有这条，_maybe_restart_for_ram 就是个没人调用的死函数——而它失效
        的方式是静默的：照常出图，只是内存一路涨到卡死。
        """
        calls = []
        job = mock.Mock()

        def _take():
            if calls.count("take"):
                raise SystemExit            # 让 worker 线程干净退出
            calls.append("take")
            return job

        for name, repl in (("_take", _take),
                           ("process", lambda j: calls.append("process")),
                           ("_finish", lambda j: calls.append("finish")),
                           ("_maybe_restart_for_ram",
                            lambda: calls.append("ram"))):
            p = mock.patch.object(image_jobs, name, repl)
            p.start()
            self.addCleanup(p.stop)

        t = threading.Thread(target=image_jobs._worker, daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "worker 没按预期退出")
        self.assertEqual(calls, ["take", "process", "finish", "ram"])


if __name__ == "__main__":
    unittest.main()
