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
from app import nai
from app import qq_api
from app import skills
from app import image_out
from app.config import QQ_AGENT_ID
from app.tools.normal import generate_image


class _FakeTime:
    """替掉 image_jobs 眼里的 time。

    只换模块属性，不动 stdlib 的 time——后者会影响整个进程（含 mock 自己）。
    clock 默认是**定值**（防抖 / 冷却判定用）；要跑 _restart_comfy 的轮询循环
    时传一个会走的时钟进去，否则 `while time.time() - start < timeout` 永远成立。
    """

    def __init__(self, clock=None):
        self._clock = clock or (lambda: 1000.0)
        self.slept = []

    def time(self):
        return self._clock()

    def sleep(self, seconds):
        self.slept.append(seconds)


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
                             ("_wait_comfy_idle", lambda timeout=90: True),
                             # 提交前会查一次显存水位，挡掉（要测那条的见
                             # ReleaseOnLowVramTest，它自己装返回值）
                             ("_free_vram_gb", lambda: None)):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _enqueue(self, ctx=("group", "9"), wf=None, skill=None):
        return image_jobs.enqueue(ctx[0], ctx[1], wf or {"1": {}}, skill)


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

    def test_nai_depth_counts_only_nai(self):
        """状态栏的数据源：nai_depth 只数 NAI 的在跑/在排，别的渠道不算。"""
        self._enqueue(("group", "9"))                    # 普通渠道
        self._enqueue(("group", "9"), wf="cat", skill="nai")
        self.assertEqual(image_jobs.nai_depth(), (0, 1))
        image_jobs._take_nowait()          # 跑起来的是普通渠道那张
        self.assertEqual(image_jobs.nai_depth(), (0, 1))
        image_jobs._take_nowait()          # NAI 那张开跑
        self.assertEqual(image_jobs.nai_depth(), (1, 0))

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


class SkillPriorityTest(unittest.TestCase):
    """渠道权重：qwen 是唯一的重渠道，其余一律 1。"""

    def test_qwen_is_heavy(self):
        self.assertGreater(skills.skill_priority("qwen_image_v1"), 1)

    def test_default_and_unknown_are_normal(self):
        """默认渠道、拼错的名字、写作类 skill —— 全都按普通活处理。

        「不认识就当普通活」是刻意的：权重写错方向（把普通渠道当成重的）
        会让它被无谓地延后，而延后一次就是让对方多等一张图的时间。
        """
        for name in ("anima", "krea2", "image_gen_v1", "goutoujunshi",
                     "没这个skill", "", None):
            self.assertEqual(skills.skill_priority(name), 1, name)

    def test_frontmatter_overrides_code_default(self):
        """skill 自己声明了 priority 就用它——给「以后再加渠道」留的口子。"""
        with mock.patch.object(skills, "load_skill", mock.Mock(return_value={
                "skill_md": "---\nkind: 生图\npriority: 7\n---\n\n# x\n"})):
            self.assertEqual(skills.skill_priority("whatever"), 7)

    def test_bogus_frontmatter_falls_back(self):
        """frontmatter 写了非数字：退回默认值，不能让一张图因为写错就发不出去。"""
        with mock.patch.object(skills, "load_skill", mock.Mock(return_value={
                "skill_md": "---\nkind: 生图\npriority: 高\n---\n\n# x\n"})):
            self.assertEqual(skills.skill_priority("whatever"), 1)

    def test_skill_with_no_md_falls_back_to_code_default(self):
        """读不到规范（目录没了 / 文件读不出来）也不能崩——按代码里的表算。"""
        with mock.patch.object(skills, "load_skill", mock.Mock(return_value=None)):
            self.assertEqual(skills.skill_priority("qwen_image_v1"), 5)

    def test_the_real_qwen_skill_md_declares_it(self):
        """真文件里那份 frontmatter 得能解析出来——不然权重只在代码里生效，
        下一个改这个目录的人看不到「它为什么排最后」。"""
        self.assertEqual(skills.skill_priority("qwen_image_v1"), 5)


class QueuePriorityTest(_Base):
    """重渠道（qwen）排最后，而且不让它连跑第二张。

    背景：qwen 一套权重 10.5GB / 空闲 10.78GB，**第 1 张必成、第 2 张必死**
    （提交后 2~6 秒 TDR，两次把整机拖重启）。外挂启动参数和更低量化都已试到底，
    所以只能从队列侧管：普通渠道永远插到它前面 + 跑完再空一个冷却窗。
    """

    def test_normal_job_jumps_ahead_of_queued_heavy(self):
        """核心诉求：qwen 先入队，但后到的 anima 先跑。"""
        heavy, _ = self._enqueue(skill="qwen_image_v1")
        normal, _ = self._enqueue(skill="anima")
        self.assertEqual(image_jobs._take_nowait(), normal)
        image_jobs._finish(normal)
        self.assertEqual(image_jobs._take_nowait(), heavy)

    def test_fifo_among_normal_jobs(self):
        """普通渠道之间还是先进先出——优先级不能把队列变成插队游戏。"""
        a, _ = self._enqueue(skill="anima")
        b, _ = self._enqueue(skill="krea2")
        self.assertEqual(image_jobs._take_nowait(), a)
        self.assertEqual(image_jobs._take_nowait(), b)

    def test_fifo_among_heavy_jobs(self):
        """两个 qwen 之间也讲先来后到（排名键的第三个字段管这个）。"""
        a, _ = self._enqueue(skill="qwen_image_v1")
        b, _ = self._enqueue(skill="qwen_image_v1")
        self.assertEqual(image_jobs._take_nowait(), a)
        self.assertEqual(image_jobs._take_nowait(), b)

    def test_ahead_of_counts_priority_order(self):
        """报给对方的「前面还有 N 张」要按**出队顺序**数。

        qwen 先入队却排在后面，按入队顺序数就会报成 0 —— 模型于是跟对方说
        「已经在画了」，而实际前面还压着一张 anima。
        """
        self._enqueue(skill="anima")
        heavy, _ = self._enqueue(skill="qwen_image_v1")
        self.assertEqual(image_jobs.ahead_of(heavy), 1)

    def test_heavy_queue_has_its_own_ceiling(self):
        """重活排队另有上限：一张 qwen 就 100 秒，排长了不如直接说画不了。"""
        for i in range(image_jobs.MAX_HEAVY_IN_QUEUE):
            # 换会话绕开 per-session 上限，专门顶重渠道这条
            job, reason = self._enqueue(("group", str(i)), skill="qwen_image_v1")
            self.assertIsNone(reason)
        job, reason = self._enqueue(("group", "999"), skill="qwen_image_v1")
        self.assertIsNone(job)
        self.assertIn("通道", reason)

    def test_heavy_ceiling_counts_the_running_one(self):
        """正在跑的那张 qwen 也算占位——否则会在它还没跑完时又灌两张进来。"""
        first, _ = self._enqueue(("group", "0"), skill="qwen_image_v1")
        self.assertEqual(image_jobs._take_nowait(), first)    # 开跑，进 _running
        for i in range(image_jobs.MAX_HEAVY_IN_QUEUE - 1):
            self._enqueue(("group", str(i + 1)), skill="qwen_image_v1")
        job, reason = self._enqueue(("group", "999"), skill="qwen_image_v1")
        self.assertIsNone(job)
        self.assertIn("通道", reason)

    def test_normal_ceiling_is_not_affected(self):
        """重渠道的上限绝不能卡到普通渠道头上。"""
        for i in range(image_jobs.MAX_HEAVY_IN_QUEUE):
            self._enqueue(("group", "h" + str(i)), skill="qwen_image_v1")
        job, reason = self._enqueue(("group", "n"), skill="anima")
        self.assertIsNone(reason)
        self.assertIsNotNone(job)


class HeavyCooldownTest(_Base):
    """一张 qwen 跑完之后的冷却窗：重渠道重新排队尾，普通渠道先上。

    冷却窗是留给残留权重散掉的——实测「第 1 张出图后显存只剩 2.35GB、
    内存只剩 4.21GB」，而第 2 张要重新摊开 6000MB 的文本编码器。
    """

    def setUp(self):
        super().setUp()
        self.clock = [1000.0]
        p = mock.patch.object(image_jobs, "time",
                              _FakeTime(lambda: self.clock[0]))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(image_jobs, "QWEN_COOLDOWN", 90.0)
        p.start()
        self.addCleanup(p.stop)

    def _run_heavy(self, ctx=("group", "9")):
        """真跑完一张 qwen——冷却窗只在「真出图」时才开。"""
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        with mock.patch.object(image_jobs, "wait_done", return_value=entry):
            job, reason = image_jobs.enqueue(ctx[0], ctx[1], {"1": {}},
                                             "qwen_image_v1")
            self.assertIsNone(reason)
            image_jobs._drain()
        return job

    def test_heavy_after_heavy_waits_out_the_cooldown(self):
        self._run_heavy()
        second, _ = self._enqueue(skill="qwen_image_v1")
        self.assertIsNone(image_jobs._take_nowait())        # 冷却中：不许开跑
        self.clock[0] += 91
        self.assertEqual(image_jobs._take_nowait(), second)

    def test_normal_job_still_runs_during_cooldown(self):
        """冷却窗不是「停摆」——它的意义正是把空隙让给别的渠道。"""
        self._run_heavy()
        normal, _ = self._enqueue(skill="anima")
        self.assertEqual(image_jobs._take_nowait(), normal)

    def test_cooldown_does_not_open_on_failure(self):
        """失败/超时那张已经把 ComfyUI 清干净了，没有残留要等——别白罚 90 秒。"""
        with mock.patch.object(image_jobs, "wait_done",
                               side_effect=TimeoutError("超时")):
            image_jobs.enqueue("group", "9", {"1": {}}, "qwen_image_v1")
            image_jobs._drain()
        nxt, _ = self._enqueue(skill="qwen_image_v1")
        self.assertEqual(image_jobs._take_nowait(), nxt)

    def test_no_cooldown_after_a_normal_job(self):
        """普通渠道跑完不开冷却——否则连画两张 anima 都要白等 90 秒。"""
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        with mock.patch.object(image_jobs, "wait_done", return_value=entry):
            image_jobs.enqueue("group", "9", {"1": {}}, "anima")
            image_jobs._drain()
        nxt, _ = self._enqueue(skill="qwen_image_v1")
        self.assertEqual(image_jobs._take_nowait(), nxt)

    def test_heavy_during_cooldown_goes_behind_normal(self):
        """冷却中的 qwen 要真的退到**队尾**，而不只是「暂时不跑」。

        推回队尾要重新取号：不重新取号的话，几个冷却中的重渠道会共用同一个
        序号，谁先谁后变成集合顺序（不确定）。
        """
        self._run_heavy()
        heavy, _ = self._enqueue(skill="qwen_image_v1")
        normal, _ = self._enqueue(skill="anima")
        image_jobs._take_nowait()          # 冷却中：先把 heavy 推到队尾，再取 normal
        self.assertEqual(list(image_jobs._queue), [heavy])   # heavy 退到队尾，normal 已出队
        self.assertEqual(heavy.waits, 1)
        self.clock[0] += 91
        self.assertEqual(image_jobs._take_nowait(), heavy)

    def test_zero_cooldown_disables_the_gate(self):
        """QWEN_COOLDOWN=0 就是「只按权重排序」，别把功能做成一开就关不掉。"""
        p = mock.patch.object(image_jobs, "QWEN_COOLDOWN", 0)
        p.start()
        self.addCleanup(p.stop)
        self._run_heavy()
        nxt, _ = self._enqueue(skill="qwen_image_v1")
        self.assertEqual(image_jobs._take_nowait(), nxt)

    def test_wait_counter_records_the_yields(self):
        """被让行几次要留痕——出问题时这是唯一能看出「qwen 被推了几次」的地方。"""
        self._run_heavy()
        heavy, _ = self._enqueue(skill="qwen_image_v1")
        self.assertEqual(heavy.waits, 0)
        self.assertIsNone(image_jobs._take_nowait())   # 冷却中，推回队尾
        self.assertEqual(heavy.waits, 1)


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
            # 提交前查显存水位——不挡就会真去 GET 真机的 /system_stats
            ("_free_vram_gb", lambda: None),
            # 超时路径会真去轮询 /queue 等它退场：ComfyUI 离线时这里要干等
            # 90 秒（一个用例就把整个模块拖到 110 秒）。_Base 早就挡了，这个
            # 类漏了。
            ("_wait_comfy_idle", lambda timeout=90: True),
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


class ReleaseOnLowVramTest(unittest.TestCase):
    """显存低于水位就先 /free 再提交（2026-09-27 加）。

    针对的场景：ComfyUI **从不把上一个任务清干净**——日志里那句
    `Unloaded partially: 2896.25 MB freed, 1591.04 MB remains loaded` 就是
    证据，残留 1.6~2.1GB 会一路叠上去。anima（峰值约 5.4GB）扛得住，但 qwen
    一张就要 11.1GB / 11.94GB，连画第三张就触发 nvlddmkm 153、进程消失
    （16:09 成 / 16:12 成 / 16:14 崩）。

    「换渠道先 /free」那条规则管不到「同渠道连画」，所以补这一条。
    """

    def setUp(self):
        image_jobs._reset()
        self.free_calls = []
        self.posts = []

    def _patch(self, vram):
        """vram = _free_vram_gb 的返回值（None 表示问不到）。"""
        def _fake_free():
            self.free_calls.append(vram)
            return vram

        def _post(url, json=None, timeout=None):
            self.posts.append(url)
            return mock.Mock(status_code=200)

        def _get(url, timeout=None):
            resp = mock.Mock()
            resp.json = lambda: {"devices": [{}], "system": {}}
            return resp

        for name, repl in (("_free_vram_gb", _fake_free),
                           ("requests", mock.Mock(get=_get, post=_post))):
            p = mock.patch.object(image_jobs, name, repl)
            p.start()
            self.addCleanup(p.stop)

    def _threshold(self, gb):
        p = mock.patch.object(image_jobs, "COMFY_MIN_FREE_VRAM_GB", gb)
        p.start()
        self.addCleanup(p.stop)

    def test_above_threshold_does_not_free(self):
        """anima 跑完还剩约 5.5GB——不该动它，模型保持热的。"""
        self._patch(8.0)
        self._threshold(5.0)
        image_jobs._maybe_release_for_low_vram()
        self.assertEqual(self.posts, [])

    def test_below_threshold_frees(self):
        """qwen 跑完只剩约 0.8GB——下一张必须先清。"""
        self._patch(0.8)
        self._threshold(5.0)
        image_jobs._maybe_release_for_low_vram()
        self.assertEqual(self.posts, [image_jobs.COMFYUI_URL + "/free"])

    def test_exactly_at_threshold_does_not_free(self):
        """判据是「低于」水位才动手，等于水位不算。"""
        self._patch(5.0)
        self._threshold(5.0)
        image_jobs._maybe_release_for_low_vram()
        self.assertEqual(self.posts, [])

    def test_threshold_is_the_vram_knob_not_the_ram_one(self):
        """读的必须是显存水位，不是内存水位——两个水位名字只差一个词。

        把内存水位钉到 1.0、显存水位定成 8.0、显存剩 5.0：正确实现该清
        （5.0 < 8.0），读错配置的话 5.0 >= 1.0 就漏过去了。这条专门用来
        区分「读了哪个配置」——别的测试区分不出来，顶部那个「关掉」的早退
        会先把它们挡住。
        """
        self._patch(5.0)
        self._threshold(8.0)
        p = mock.patch.object(image_jobs, "COMFY_MIN_FREE_RAM_GB", 1.0)
        p.start()
        self.addCleanup(p.stop)
        image_jobs._maybe_release_for_low_vram()
        self.assertEqual(self.posts, [image_jobs.COMFYUI_URL + "/free"])

    def test_zero_threshold_disables_the_feature(self):
        self._patch(0.1)
        self._threshold(0)
        image_jobs._maybe_release_for_low_vram()
        self.assertEqual(self.posts, [])

    def test_disabled_does_not_even_query(self):
        """关掉时连查都不该查——省一次没用的 HTTP。"""
        self._patch(0.1)
        self._threshold(0)
        image_jobs._maybe_release_for_low_vram()
        self.assertEqual(self.free_calls, [])

    def test_unreachable_stats_does_not_free(self):
        """问不到就什么都别做——宁可照常提交，也别拿猜的水位瞎清。"""
        self._patch(None)
        self._threshold(5.0)
        image_jobs._maybe_release_for_low_vram()
        self.assertEqual(self.posts, [])

    def test_process_frees_before_submitting(self):
        """调用点必须在提交**之前**——顺序反了就等于没清。"""
        self._threshold(5.0)
        order = []

        def _spy():
            order.append("free")

        def _queue(workflow):
            order.append("submit")
            raise RuntimeError("提交失败，后面不用跑")

        for name, repl in (("_maybe_release_for_low_vram", _spy),
                           ("_queue_prompt", _queue),
                           ("_notice", lambda job, stage="submit": None)):
            p = mock.patch.object(image_jobs, name, repl)
            p.start()
            self.addCleanup(p.stop)

        image_jobs.process(image_jobs.Job("private", "1", {}, None))
        self.assertEqual(order, ["free", "submit"])


class DisabledChannelTest(unittest.TestCase):
    """停用渠道的硬闸：模型点名也没用，而且一步都不该碰 ComfyUI。

    qwen_image_v1 在这台机器上是「单张就能把整机拖崩」——2026-09-27 实测五次、
    三次整机重启，最后一次队列里**只排了它一张**（`ahead_of=0`）。所以它必须有
    一道**代码级**的闸：光靠「不进 skills 白名单」挡不住，白名单只管提示词里列
    不列，模型记得这个名字照样能把 skill 传进来。
    """

    def setUp(self):
        image_jobs._reset()
        self.comfy = mock.Mock(return_value=True)
        self.load = mock.Mock(return_value={"workflow": {"1": {}},
                                            "character": ""})
        for target, repl in (("comfy_alive", self.comfy),
                             ("_ensure_worker", lambda: None),
                             ("_send_image", mock.Mock()),
                             ("_send_text", mock.Mock()),
                             ("_free_vram_gb", lambda: None)):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)
        for target, repl in (("load_skill", self.load),
                             ("_qq_gate", mock.Mock(return_value=None)),
                             ("is_cancelled", mock.Mock(return_value=False))):
            p = mock.patch.object(generate_image, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _disabled(self, names):
        """换掉停用清单——不 patch 就得改真配置才能测「恢复」那条。"""
        p = mock.patch.object(generate_image, "DISABLED_IMAGE_SKILLS", names)
        p.start()
        self.addCleanup(p.stop)

    def _call(self, **kw):
        with mock.patch.object(qq_api, "current_context",
                               return_value=("group", "9")):
            return generate_image.tool["function"](**kw)

    def test_named_qwen_is_refused(self):
        self._disabled(["qwen_image_v1"])
        out = self._call(prompt="a cat", skill="qwen_image_v1")
        self.assertIn("停用", out)
        self.assertFalse(self.comfy.called)     # 一步都没碰 ComfyUI
        self.assertFalse(self.load.called)      # 连 skill 都没去读

    def test_i2i_is_refused_even_without_naming_qwen(self):
        """只给 source_image 不给 skill 的那条路**也必须挡住**——而且挡它的是
        另一道闸（图生图整体停用），跟 qwen 停不停无关。

        这是最容易漏的一条：模型的意图是「改图」，不是「用 qwen」，所以它不会
        传 skill，闸要是只看 skill 参数就漏过去了。
        """
        self._disabled(["qwen_image_v1"])
        out = self._call(prompt="把衣服换成红色", source_image="1")
        self.assertIn("source_image", out)
        self.assertFalse(self.comfy.called)

    def test_i2i_is_refused_with_qwen_enabled(self):
        """把 qwen 放回来也照样拒——停用的是「图生图」这个功能，不是那个渠道。

        两道闸是分开的：`_I2I_SKILLS` 管「图生图能不能跑」，`DISABLED_IMAGE_
        SKILLS` 管「这个渠道能不能用」。这条用例把后者清空，只剩前者生效。
        """
        self._disabled([])
        out = self._call(prompt="把衣服换成红色", source_image="1")
        self.assertIn("source_image", out)
        self.assertFalse(self.comfy.called)     # 连探活都没做

    def test_refusal_tells_the_model_what_to_do(self):
        """拒收不能只说「不行」——模型得知道下一步该干嘛，否则它会开始编。"""
        self._disabled(["qwen_image_v1"])
        out = self._call(prompt="a cat", skill="qwen_image_v1")
        self.assertIn("anima", out)             # 给出可用的替代
        self.assertIn("别跟对方提", out)          # 别把渠道名甩给用户

    def test_other_channels_are_untouched(self):
        """闸只挡停用的那个，别的渠道照常走。"""
        self._disabled(["qwen_image_v1"])
        out = self._call(prompt="a cat", skill="anima")
        self.assertNotIn("停用", out)
        self.assertTrue(self.comfy.called)      # 正常路径照旧会探活

    def test_empty_list_re_enables_it(self):
        """开关清空就恢复——证明这是配置项，不是写死的判断。"""
        self._disabled([])
        out = self._call(prompt="a cat", skill="qwen_image_v1")
        self.assertNotIn("停用", out)
        self.assertTrue(self.comfy.called)

    def test_named_krea2_is_refused_too(self):
        """krea2 和 qwen 一样是硬件跑不动，同一条闸管住。"""
        self._disabled(["qwen_image_v1", "krea2"])
        out = self._call(prompt="a cat", skill="krea2")
        self.assertIn("停用", out)
        self.assertFalse(self.comfy.called)
        self.assertFalse(self.load.called)

    def test_real_config_disables_the_unrunnable_channels(self):
        """真配置里 qwen + krea2 必须是停用的——这是用户机器的硬事实。

        万一有人把 config 的默认值改回可用，这条会立刻响。
        """
        from app.config import DISABLED_IMAGE_SKILLS
        self.assertIn("qwen_image_v1", DISABLED_IMAGE_SKILLS)
        self.assertIn("krea2", DISABLED_IMAGE_SKILLS)

    def test_real_config_keeps_the_runnable_channels(self):
        """anima / anima_2 / SD 是**能跑**的渠道，绝不能被误列进停用清单
        （那样就一张图都画不出了，或者两段采样那条再也点不出来）。"""
        from app.config import DISABLED_IMAGE_SKILLS
        for name in ("anima", "anima_2", "image_gen_v1"):
            self.assertNotIn(name, DISABLED_IMAGE_SKILLS, name)


class QqInvisibleTest(unittest.TestCase):
    """停用的渠道要**在 QQ 侧看不见**，不只是调用时被拒。

    两件事分开：① 白名单不列出（模型不会想起来）；② 描述里不再教怎么用
    （模型记得名字也不会被「指路」）。只做前者挡不住，只做后者也挡不住
    —— 09-27 已经吃过一次（光靠白名单挡不住模型传参）。
    """

    DISABLED = ("qwen_image_v1", "krea2")
    RUNNABLE = ("anima", "anima_2", "image_gen_v1")

    def test_qq_whitelist_hides_disabled_channels(self):
        from app import agents
        for name in self.DISABLED:
            self.assertFalse(agents.allows_skill("qq", name), name)
        for name in self.RUNNABLE:
            self.assertTrue(agents.allows_skill("qq", name), name)

    def test_qq_skill_list_does_not_advertise_them(self):
        from app.agent_prompt import _build_skill_list
        block = _build_skill_list("qq")
        for name in self.DISABLED:
            self.assertNotIn("**" + name + "**", block, name)
        for name in self.RUNNABLE:
            self.assertIn("**" + name + "**", block, name)

    def test_qq_description_still_mentions_them_only_to_refuse(self):
        """描述里出现 qwen/krea2 是**允许**的——但只许出现在「已停用、不要传」
        的语境里，不许再有「说 krea2 就传 skill=krea2」这种指路话。"""
        from app.tools.normal.generate_image import tool
        from app.config import QQ_AGENT_ID
        desc = tool["description_overrides"][QQ_AGENT_ID]
        for name in self.DISABLED:
            self.assertIn(name, desc, name)          # 得让模型知道「点了也没用」
        self.assertIn("不要传 skill=qwen_image_v1 或 skill=krea2", desc)
        # 不能再教怎么调它们
        self.assertNotIn("传 skill=krea2", desc)
        self.assertNotIn("krea2 传", desc)
        self.assertNotIn("说 krea2", desc)


class CleanStartTest(_Base):
    """「开跑前先要一个干净的 ComfyUI」（2026-09-27 加，见
    image_jobs.CLEAN_START_SKILLS）。

    场景：anima_2 两段采样要摊开 底模 3988MB + TE 1136MB + VAE 241MB ≈ 5.4GB，
    而上一张 anima 跑完（打过 /free 也一样）只剩约 5.5GB——余量太薄，不重启
    容易 180 秒超时。刚起来的 ComfyUI 有 10.8GB，够。

    2026-09-29：阈值从 8.0 下调到 6.0。原先的 8.0 是「双底模」时代的数——那时
    工作流挂两块底模（ani11 + realskin），峰值 ≈9.4GB；现在两段共用一块
    realskin，峰值砍半到 ≈5.4GB（≈ 单底模 anima），8.0 已明显过保守。
    """

    def setUp(self):
        super().setUp()
        self.restarts = []
        p = mock.patch.object(image_jobs, "_restart_comfy",
                              lambda *a, **k: self.restarts.append(True) or True)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, skill, vram):
        """跑一张 skill 渠道的图；vram = 提交前 ComfyUI 报的空闲显存。"""
        p = mock.patch.object(image_jobs, "_free_vram_gb", lambda: vram)
        p.start()
        self.addCleanup(p.stop)
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        with mock.patch.object(image_jobs, "wait_done", return_value=entry):
            self._enqueue(skill=skill)
            image_jobs._drain()

    def test_dirty_comfyui_restarts_before_heavy_channel(self):
        """anima_2 撞上 5.6GB 的脏状态：先重启，再提交，图照样画完。"""
        self._run("anima_2", 5.6)
        self.assertEqual(self.restarts, [True])
        self.assertEqual(len(self.sent_images), 1)

    def test_clean_comfyui_does_not_restart(self):
        """刚起来 10.8GB：没必要为它多花 60~90 秒重新加载模型。"""
        self._run("anima_2", 10.8)
        self.assertEqual(self.restarts, [])

    def test_normal_channel_never_restarts(self):
        """普通渠道不受影响——anima 单底模 5.4GB 有自己的 /free 水位兜着，
        为它重启只是白等一分钟。"""
        self._run("anima", 0.8)
        self.assertEqual(self.restarts, [])

    def test_unknown_vram_does_not_restart(self):
        """显存问不到就别折腾：那种情况 ComfyUI 多半已经不在了，重启请求
        同样发不出去——照常提交，让 _notice 去说「ComfyUI 没在线」。"""
        self._run("anima_2", None)
        self.assertEqual(self.restarts, [])
        self.assertEqual(len(self.sent_images), 1)

    def test_restart_happens_before_submitting(self):
        """顺序反了等于没清：必须是「先重启 → 再提交」。"""
        order = []
        for name, repl in (("_restart_comfy",
                            lambda *a, **k: order.append("restart") or True),
                           ("_queue_prompt",
                            lambda wf: order.append("submit") or "pid")):
            p = mock.patch.object(image_jobs, name, repl)
            p.start()
            self.addCleanup(p.stop)
        self._run("anima_2", 5.6)
        self.assertEqual(order, ["restart", "submit"])

    def test_at_most_one_restart_per_job(self):
        """每张图最多重启一次——不能退化成重启循环。"""
        self._run("anima_2", 0.5)
        self.assertEqual(len(self.restarts), 1)

    def test_threshold_sits_above_the_measured_dirty_vram(self):
        """阈值必须高于「anima 跑完的实测残值 5.6GB」，否则这条规则永不触发。

        这条是「常量被随手改小」的锁：改成 5.0 就得红。
        """
        self.assertIn("anima_2", image_jobs.CLEAN_START_SKILLS)
        self.assertGreater(image_jobs.CLEAN_START_SKILLS["anima_2"], 5.6)


class NaiCloudTest(_Base):
    """NAI 云端分支：完全不碰 ComfyUI，图由 NovelAI 出，worker 发回原群。"""

    def setUp(self):
        super().setUp()
        self.nai_calls = []
        p = mock.patch.object(nai, "generate",
                             lambda prompt: self.nai_calls.append(prompt)
                             or b"PNGDATA")
        p.start()
        self.addCleanup(p.stop)
        self.nai_sent = []          # qq_api.send_image 捕获（cloud 分支走这条）
        p = mock.patch.object(qq_api, "send_image",
                             lambda t, tid, path: self.nai_sent.append(
                                 (t, tid, path)))
        p.start()
        self.addCleanup(p.stop)
        # save_bytes 会真写盘；这里替成固定路径，测试保持密闭（不落真实文件）。
        p = mock.patch.object(image_out, "save_bytes",
                             lambda data, ext="png", stem="img": "/fake/nai.png")
        p.start()
        self.addCleanup(p.stop)

    def test_nai_sends_without_comfyui(self):
        """一张 nai：调用 NAI、写盘、发回群；ComfyUI 一点没碰。"""
        self._enqueue(("group", "9"), wf="a cat prompt", skill="nai")
        image_jobs._drain()
        self.assertEqual(self.nai_calls, ["a cat prompt"])
        self.assertEqual(len(self.nai_sent), 1)
        # ComfyUI 那条发图 / 提交路径都没走
        self.assertEqual(len(self.sent_images), 0)
        self.assertEqual(image_jobs.queue_depth(), 0)

    def test_nai_failure_notifies_not_sends(self):
        """NAI 调用挂了：发一句说明，但不发图、不假装成功。"""
        p = mock.patch.object(nai, "generate",
                             mock.Mock(side_effect=RuntimeError("NAI 挂了")))
        p.start()
        self.addCleanup(p.stop)
        self._enqueue(("group", "9"), wf="x", skill="nai")
        image_jobs._drain()
        self.assertEqual(len(self.nai_sent), 0)
        self.assertTrue(any("NAI" in t for _, _, t in self.sent_texts))


class NaiRoutingTest(unittest.TestCase):
    """generate_image：skill=nai 的分流 + 开关判定（不动 ComfyUI）。"""

    def setUp(self):
        for target, repl in (("_qq_gate", mock.Mock(return_value=None)),
                             ("is_cancelled", mock.Mock(return_value=False))):
            p = mock.patch.object(generate_image, target, repl)
            p.start()
            self.addCleanup(p.stop)
        for target, repl in (("_send_image", mock.Mock()),
                             ("_send_text", mock.Mock()),
                             ("_ensure_worker", lambda: None),
                             ("comfy_alive", mock.Mock(return_value=True)),
                             ("_free_vram_gb", lambda: None),
                             ("_wait_comfy_idle", lambda timeout=90: True)):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(image_jobs.Job, "wait",
                             lambda self, poll=2: self.entry)
        p.start()
        self.addCleanup(p.stop)

    def _call(self, skill, ctx, nai_ok=True, nai_reason=""):
        with mock.patch.object(agents, "nai_allowed",
                               return_value=(nai_ok, nai_reason)), \
                mock.patch.object(qq_api, "current_context", return_value=ctx), \
                mock.patch.object(image_jobs, "enqueue",
                                 return_value=(mock.Mock(done=mock.Mock()),
                                               None)) as eq, \
                mock.patch.object(image_jobs, "ahead_of", return_value=0):
            out = generate_image.tool["function"]("a cat", skill=skill)
            return out, eq

    def test_nai_refused_when_not_allowed(self):
        out, eq = self._call("nai", ("group", "9"), nai_ok=False,
                            nai_reason="本群未开通 NAI")
        self.assertIn("本群未开通 NAI", out)
        self.assertFalse(eq.called)              # 没入队

    def test_nai_enqueues_when_allowed(self):
        out, eq = self._call("nai", ("group", "9"), nai_ok=True)
        self.assertIn("已经在画了", out)
        self.assertTrue(eq.called)
        _, kwargs = eq.call_args
        self.assertEqual(kwargs.get("skill"), "nai")

    def test_nai_web_refused(self):
        out, eq = self._call("nai", (None, None), nai_ok=True)
        self.assertIn("仅支持 QQ", out)
        self.assertFalse(eq.called)

    def test_description_still_a_string(self):
        # 改 description 容易把隐式字符串拼接弄成 tuple（见模块注释）。
        self.assertIsInstance(generate_image.tool["description"], str)
        self.assertIsInstance(
            generate_image.tool["description_overrides"][QQ_AGENT_ID], str)
        self.assertIn("nai", generate_image.tool["description"])


class RecentOutcomesTest(_Base):
    """生图回执：模型提交之后就再也收不到消息，全靠 recent_line 知道结果。

    这是 2026-09-29 用户提的「AI 老是说要重新帮忙跑图」的正面修法——回执必须
    真反映「出没出图」，也必须只报本会话的，否则模型会拿别群的图串台。
    """

    def _entry(self, names=("a.png",)):
        return {"outputs": {"9": {"images": [{"filename": n} for n in names]}}}

    def test_success_recorded_and_rendered(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue(skill="anima")
            image_jobs._drain()
        line = image_jobs.recent_line("group", "9")
        self.assertIn("已完成：", line)
        self.assertIn("已出图（anima）", line)
        # 带完成时刻：模型要能分清「刚才那张」和「很久以前那张」
        self.assertRegex(line, r"\d\d:\d\d 已出图")
        # 明说「没列出来的 = 还没提交」——这是「AI 撒谎」的正面修法
        self.assertIn("还没提交", line)
        # 旧措辞「别再问要不要重画」会把「对方说没看到 → 该重跑」堵死
        self.assertNotIn("别再问", line)
        self.assertIn("重跑", line)

    def test_failure_recorded_with_reason(self):
        with mock.patch.object(image_jobs, "wait_done",
                               side_effect=TimeoutError("超时")), \
                mock.patch.object(image_jobs, "requests",
                                  mock.Mock(post=lambda *a, **k: None)):
            self._enqueue(skill="anima")
            image_jobs._drain()
        line = image_jobs.recent_line("group", "9")
        self.assertIn("失败（anima：超时）", line)

    def test_only_the_last_three_and_newest_is_last(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            for _ in range(4):
                self._enqueue(skill="anima")
                image_jobs._drain()
        items = image_jobs.recent_outcomes("group", "9", 3)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(r["ok"] for r in items))
        self.assertIn("最新在后", image_jobs.recent_line("group", "9", 3))

    def test_inflight_job_is_reported_as_not_yet_drawn(self):
        """还没跑完的任务也要报出来——否则模型分不清「刚提交」和「早跑完」，
        会把上一条已完成当成对方刚发的那张（2026-09-29 群 1041079621）。"""
        self._enqueue(skill="nai")          # 不 _drain：留在队列里
        line = image_jobs.recent_line("group", "9")
        self.assertIn("还没出图", line)
        self.assertIn("排队中", line)
        self.assertNotIn("已出图", line)

    def test_inflight_of_other_session_is_not_visible(self):
        self._enqueue(("group", "8"), skill="nai")
        self.assertEqual(image_jobs.recent_line("group", "9"), "")
        self.assertIn("还没出图", image_jobs.recent_line("group", "8"))

    def test_other_sessions_are_not_visible(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue(("group", "9"), skill="anima")
            image_jobs._drain()
        self.assertNotEqual(image_jobs.recent_line("group", "9"), "")
        self.assertEqual(image_jobs.recent_line("group", "8"), "")

    def test_web_target_has_no_recall(self):
        # 网页侧同步等结果，模型直接从工具返回值就知道成没成，不需要回执
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue((None, None), skill="anima")
            image_jobs._drain()
        self.assertEqual(image_jobs.recent_outcomes(None, None), [])
        self.assertEqual(image_jobs.recent_line(None, None), "")

    def test_empty_when_nothing_ran(self):
        self.assertEqual(image_jobs.recent_line("group", "9"), "")


if __name__ == "__main__":
    unittest.main()
