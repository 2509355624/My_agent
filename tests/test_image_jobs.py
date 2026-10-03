"""生图队列 + generate_image 的排队/异步分流测试。

原则：零网络、零显卡、零真实线程等待。ComfyUI 的 HTTP、QQ 发送端、worker
线程全 mock，只测判定逻辑（谁排队、谁被拒、超时怎么收场、失败怎么回话）。
"""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import app.agents as agents
from app import image_jobs
from app import image_log
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
        # 编号是贴在图上的 caption，单独记一份：sent_images 保持 (target, tid,
        # path) 三元组，别为了加一列去动那一堆 [:2] / [2] 的老断言。
        self.sent_captions = []
        # seed 也单独记一份：它决定 caption 上那行字有没有最后一段。
        self.sent_seeds = []

        def _fake_image(target, tid, url, tag="", skill="", seed=None):
            self.sent_images.append((target, tid, url))
            self.sent_captions.append(tag)
            self.sent_seeds.append(seed)

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

    def _enqueue(self, ctx=("group", "9"), wf=None, skill=None, seed=None):
        return image_jobs.enqueue(ctx[0], ctx[1], wf or {"1": {}}, skill,
                                  seed=seed)


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
        for name in ("anima_soft", "anima_gloss", "krea2", "image_gen_v1",
                     "nffa", "goutoujunshi", "没这个skill", "", None):
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
        normal, _ = self._enqueue(skill="anima_soft")
        self.assertEqual(image_jobs._take_nowait(), normal)
        image_jobs._finish(normal)
        self.assertEqual(image_jobs._take_nowait(), heavy)

    def test_fifo_among_normal_jobs(self):
        """普通渠道之间还是先进先出——优先级不能把队列变成插队游戏。"""
        a, _ = self._enqueue(skill="anima_soft")
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
        self._enqueue(skill="anima_soft")
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
        job, reason = self._enqueue(("group", "n"), skill="anima_soft")
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
        normal, _ = self._enqueue(skill="anima_soft")
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
            image_jobs.enqueue("group", "9", {"1": {}}, "anima_soft")
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
        normal, _ = self._enqueue(skill="anima_soft")
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

    def test_the_image_carries_its_tag(self):
        """出图那张要把自己的编号带上——worker 发的就是入队时定的那个。"""
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            job, _ = self._enqueue()
            image_jobs._drain()
        self.assertEqual(self.sent_captions, [job.tag])
        self.assertRegex(job.tag, r"^HT-\d{8}-\d{6}-\d{3}$")

    def test_the_image_carries_its_seed(self):
        """种子同样由 worker 原样转发：它在入队那一刻就定死了。

        worker 拿不到工作流里那个数（seed 混在几十个节点里），所以 caption 上
        那一段只能靠 Job.seed 这份快照——转发错了，对方抄下来的就是假种子。
        """
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue(seed=4100493889)
            image_jobs._drain()
        self.assertEqual(self.sent_seeds, [4100493889])

    def test_no_seed_reaches_the_sender_as_none(self):
        """没种子（NAI 云端图 / 老调用方）就传 None——caption 上那段不写。"""
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue()
            image_jobs._drain()
        self.assertEqual(self.sent_seeds, [None])

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
        """图发出去失败也算失败：照样说一句，不让异常冒出 worker 线程。

        话术必须是「图画好了、只是没发出去」——**不能说「图没画出来」**：
        走到这一步图已经画好了，说没画出来是撒谎（2026-10-01 03:44
        Anima_00276_.png 那次就是这么骗人的）。
        """
        p = mock.patch.object(image_jobs, "_send_image",
                              side_effect=OSError("down"))
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue()
            image_jobs._drain()
        self.assertEqual(len(self.sent_texts), 1)
        self.assertIn("图画好了", self.sent_texts[0][2])
        self.assertNotIn("图没画出来", self.sent_texts[0][2])

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

    def _capture_captions(self):
        from app import image_out
        sent = []
        return mock.patch.object(image_out, "prepare_for_send",
                                 lambda f, fmt="jpg": "C:/tmp/y.jpg"), \
            mock.patch.object(qq_api, "send_image",
                              lambda target, tid, path, caption="":
                              sent.append((path, caption))), sent

    def test_tag_goes_on_the_message_as_caption(self):
        """编号必须和图片在**同一条消息**里。

        这是整条链路的前提：群友引用这条消息时，qq_bot 会把正文拉回来
        （_resolve_quote → get_msg），编号才跟着引用重新进入模型输入。
        拆成两条消息的话，引用只会拿到空正文，编号就丢了。
        """
        p1, p2, sent = self._capture_captions()
        with p1, p2:
            image_jobs._send_image("group", "9", "b.png", "HT-20261001-074112-384")
        self.assertEqual(sent, [("C:/tmp/y.jpg", "HT-20261001-074112-384")])

    def test_no_tag_means_no_caption(self):
        """不传编号 = 不加那行字，行为与从前完全一致（老调用方不受影响）。"""
        p1, p2, sent = self._capture_captions()
        with p1, p2:
            image_jobs._send_image("group", "9", "b.png")
        self.assertEqual(sent, [("C:/tmp/y.jpg", "")])

    def test_caption_carries_size_and_skill(self):
        """编号后面跟分辨率、渠道——编号仍在前且原样（引用靠它被抠回来）。"""
        from app import image_out
        p1, p2, sent = self._capture_captions()
        with p1, p2, \
             mock.patch.object(image_out, "local_size", return_value="1024×1536"):
            image_jobs._send_image("group", "9", "b.png",
                                   "HT-20261001-074112-384", skill="anima_soft")
        self.assertEqual(
            sent, [("C:/tmp/y.jpg", "HT-20261001-074112-384 · 1024×1536 · anima_soft")])

    def test_caption_survives_missing_size(self):
        """量不出分辨率（图读不出来 / 回落到 ComfyUI URL）时只少这一项，别多出空档。"""
        from app import image_out
        p1, p2, sent = self._capture_captions()
        with p1, p2, \
             mock.patch.object(image_out, "local_size", return_value=""):
            image_jobs._send_image("group", "9", "b.png",
                                   "HT-20261001-074112-384", skill="anima_soft")
        self.assertEqual(sent, [("C:/tmp/y.jpg", "HT-20261001-074112-384 · anima_soft")])

    def test_audit_block_sends_nothing_at_all(self):
        """被审核拦下时连编号也不发——没图却挂个编号，比什么都不发更糟。"""
        from app import image_audit
        p1, p2, sent = self._capture_captions()
        with p1, p2, mock.patch.object(image_audit, "allow_send",
                                       return_value=False):
            ok = image_jobs._send_image("group", "9", "b.png",
                                        "HT-20261001-074112-384")
        self.assertFalse(ok)
        self.assertEqual(sent, [])

    def test_caption_carries_the_seed(self):
        """seed 排在最后一段：编号还在最前，引用时照样被 TAG_RE 抠回来。

        这一行是给「拿种子改提示词重画」用的（2026-10-02 用户提）：caption 和
        图片在同一条消息里，群友引用它时整行回到模型眼前，种子于是自己就回来了
        ——不依赖模型记不记得。
        """
        from app import image_out
        p1, p2, sent = self._capture_captions()
        with p1, p2, \
             mock.patch.object(image_out, "local_size", return_value="1024×1536"):
            image_jobs._send_image("group", "9", "b.png",
                                   "HT-20261001-074112-384",
                                   skill="anima_soft", seed=4100493889)
        self.assertEqual(
            sent, [("C:/tmp/y.jpg",
                    "HT-20261001-074112-384 · 1024×1536 · anima_soft "
                    "· seed 4100493889")])

    def test_caption_writes_seed_zero(self):
        """0 是一个合法种子，不是「没传」——用 `if seed:` 判就把这张的种子弄丢了。"""
        from app import image_out
        p1, p2, sent = self._capture_captions()
        with p1, p2, \
             mock.patch.object(image_out, "local_size", return_value=""):
            image_jobs._send_image("group", "9", "b.png",
                                   "HT-20261001-074112-384", seed=0)
        self.assertEqual(sent, [("C:/tmp/y.jpg", "HT-20261001-074112-384 · seed 0")])

    def test_caption_without_seed_says_nothing_about_it(self):
        """没种子时不写「seed 空」之类的占位——整行与加这个功能之前一致。"""
        p1, p2, sent = self._capture_captions()
        with p1, p2:
            image_jobs._send_image("group", "9", "b.png", "HT-20261001-074112-384")
        self.assertNotIn("seed", sent[0][1])


class ElapsedTest(unittest.TestCase):
    """日志里的耗时只算「开跑到发出去」，排队时间不算——否则排在第 5 位的
    那张，日志会写着它画了十分钟，实际是等了十分钟。"""

    def _job(self):
        return image_jobs.Job("group", "9", {"1": {}})

    def test_counts_from_start_not_enqueue(self):
        job = self._job()
        job.created = 1000.0 - 600      # 排了十分钟
        job.started = 1000.0 - 42       # 真正画了 42 秒
        with mock.patch.object(image_jobs, "time", _FakeTime(lambda: 1000.0)):
            self.assertAlmostEqual(image_jobs._elapsed(job), 42.0)

    def test_falls_back_to_enqueue_when_never_started(self):
        """异常路径没打上 started 时退回入队时刻——宁可多算，也别报负数。"""
        job = self._job()
        job.created = 1000.0 - 7
        with mock.patch.object(image_jobs, "time", _FakeTime(lambda: 1000.0)):
            self.assertAlmostEqual(image_jobs._elapsed(job), 7.0)


class JobTagTest(unittest.TestCase):
    """Job 的编号：入队那一刻定下来，之后不再变。

    编号在**入队**时生成而不是发图时生成，是为了让「发图贴的号」和「以后写
    进账本的号」必然是同一个值——两处各自生成的话，中间隔着一两分钟，号就
    对不上了。
    """

    def setUp(self):
        image_jobs._reset()
        p = mock.patch.object(image_jobs, "_ensure_worker", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def test_generated_for_every_job(self):
        job = image_jobs.Job("group", "1", {})
        self.assertRegex(job.tag, r"^HT-\d{8}-\d{6}-\d{3}$")

    def test_explicit_tag_is_kept(self):
        """测试要固定编号时传得进来。"""
        job = image_jobs.Job("group", "1", {}, tag="HT-20261001-074112-384")
        self.assertEqual(job.tag, "HT-20261001-074112-384")

    def test_enqueue_keeps_the_same_tag(self):
        job, _ = image_jobs.enqueue("group", "1", {})
        self.assertEqual(image_jobs._queue[-1].tag, job.tag)

    def test_the_tag_is_matchable_by_the_regex(self):
        """编号必须能被 image_log.TAG_RE 抠出来——不然引用回来也白搭。"""
        job = image_jobs.Job("group", "1", {})
        self.assertEqual(image_log.find_tags("引用这条：" + job.tag), [job.tag])


class ImageLedgerTest(unittest.TestCase):
    """图**真发出去了**才记账本。

    判据是「发出去」而不是「画出来」：没发出去的图不该有编号可查——否则对方
    拿编号问到一句提示词，手上却没有那张图，比查不到更糟。
    """

    def setUp(self):
        image_jobs._reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = image_log.PATH
        image_log.PATH = os.path.join(self.tmp.name, "image_log.jsonl")
        self.addCleanup(setattr, image_log, "PATH", self._old)
        for target, repl in (("_ensure_worker", lambda: None),
                             ("_queue_prompt", lambda wf: "pid"),
                             ("_wait_comfy_idle", lambda timeout=90: True),
                             ("_free_vram_gb", lambda: None),
                             ("_send_text", lambda t, tid, x: None)):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, sent_ok, prompt="1girl, silver hair", seed=None):
        job = image_jobs.Job("group", "9", {}, skill="hd_fast", prompt=prompt,
                             seed=seed)
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        with mock.patch.object(image_jobs, "wait_done", return_value=entry), \
                mock.patch.object(image_jobs, "_send_image",
                                  return_value=sent_ok):
            image_jobs.process(job)
        return job

    def test_prompt_is_carried_on_the_job(self):
        self.assertEqual(image_jobs.Job("g", "1", {}, prompt="abc").prompt,
                         "abc")

    def test_no_prompt_means_empty_string(self):
        """没传就是空串，不是 None——别让下游拿 None 去拼字符串。"""
        self.assertEqual(image_jobs.Job("g", "1", {}).prompt, "")

    def test_enqueue_passes_the_prompt(self):
        job, _ = image_jobs.enqueue("group", "9", {}, prompt="xyz")
        self.assertEqual(job.prompt, "xyz")

    def test_landed_image_is_recorded(self):
        job = self._run(True)
        row = image_log.lookup(job.tag)
        self.assertEqual(row["prompt"], "1girl, silver hair")
        self.assertEqual(row["file"], "a.png")
        self.assertEqual(row["skill"], "hd_fast")

    def test_the_seed_is_recorded_too(self):
        """账本里的 seed 就是那张图真正跑的那一个（动漫渠道两段共用它）。

        对方只报了编号、没引用那条消息时（比如隔了一天再来问），caption 上的
        种子早就不在模型眼前了——这时只有账本答得出「这张用的什么种子」。
        """
        job = self._run(True, seed=4100493889)
        self.assertEqual(image_log.lookup(job.tag)["seed"], 4100493889)

    def test_seed_zero_is_recorded_as_zero(self):
        self.assertEqual(image_log.lookup(self._run(True, seed=0).tag)["seed"], 0)

    def test_no_seed_records_an_empty_string(self):
        """NAI 那种云端图没种子可记：存空串，不存 None。

        跟 prompt 一样——空串意味着「这张的种子我们不知道」，而 recall_image
        要据此回一句「种子没记下」，不能让下游拿 None 去拼字符串。
        """
        self.assertEqual(image_log.lookup(self._run(True).tag)["seed"], "")

    def test_undelivered_image_is_not_recorded(self):
        """发出去失败（含被审核拦下）的图，账本里查不到。"""
        job = self._run(False)
        self.assertIsNone(image_log.lookup(job.tag))


class GenerateImageSplitTest(unittest.TestCase):
    """generate_image：QQ 侧排队即返回，网页侧同步等出图。"""

    def setUp(self):
        image_jobs._reset()
        for target, repl in (
            ("load_skill", mock.Mock(return_value={"workflow": {"1": {}}})),
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
        """排队时要告诉模型前面还有几张，好让它跟对方交代一句。

        第二张必须换个词：**同样的词会被查重拦下**（见
        test_the_same_request_while_in_flight_is_not_submitted_twice），
        那是另一条规则，这里要测的是排队提示本身。
        """
        with mock.patch.object(qq_api, "current_context",
                               return_value=("group", "9")), \
                mock.patch.object(image_jobs, "wait_done",
                                  return_value={"outputs": {}}):
            generate_image.tool["function"]("a cat")        # 先占住队首
            out = generate_image.tool["function"]("a dog")  # 这一张排在后面
        self.assertIn("前面还有 1 张", out)

    def test_the_same_request_while_in_flight_is_not_submitted_twice(self):
        """同一件事还没出图又被提交一遍 → 不再排第二张（2026-10-01）。

        QQ 路径提交完立刻返回，模型这轮只拿到「排上了」；下一轮它要是没看到
        那条回执（上下文被压缩，或空头承诺守卫把如实汇报判成空头承诺），就会
        再提交一次——队列里于是多出一张一模一样的图。实测 2026-10-01 08:13
        就是这么连出两张 hd_3 的。
        """
        with mock.patch.object(qq_api, "current_context",
                               return_value=("group", "9")), \
                mock.patch.object(image_jobs, "wait_done",
                                  return_value={"outputs": {}}):
            generate_image.tool["function"]("a cat")
            out = generate_image.tool["function"]("a cat")   # 原样再来一遍
        self.assertIn("没有执行", out)
        self.assertEqual(image_jobs.inflight_count("group", "9"), 1)

    def test_another_session_may_submit_the_same_request(self):
        """查重按会话隔离：别人说同一句话当然要照画。

        它照样会被排到别人后面（队列是全局串行的），所以断言的是「没被查重
        拦下」，不是「立刻开画」。
        """
        with mock.patch.object(image_jobs, "wait_done",
                                return_value={"outputs": {}}):
            with mock.patch.object(qq_api, "current_context",
                                   return_value=("group", "9")):
                generate_image.tool["function"]("a cat")
            with mock.patch.object(qq_api, "current_context",
                                   return_value=("group", "8")):
                out = generate_image.tool["function"]("a cat")
        self.assertNotIn("没有执行", out)
        self.assertEqual(image_jobs.inflight_count("group", "8"), 1)

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
            # 每张换个词：同样的词会被查重拦下，那就测不到「名额满」这条了。
            for i in range(image_jobs.MAX_INFLIGHT):
                generate_image.tool["function"]("cat %d" % i)
            out = generate_image.tool["function"]("cat over")   # 被拒
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


class SeedSubmissionTest(unittest.TestCase):
    """generate_image 的 seed 参数：能填、填进去的就是那一个数、填错当场报错。

    需求（2026-10-02 用户）：「图有多手多脚，我要拿到种子，改一下提示词，在这个
    种子的基础上再生成」。它成立的前提是**报出去的那个数字真的就是跑过的那一个**
    ——所以下面大半用例都在盯这一条：不多填、不少填、不钳位、不悄悄换一个。

    mock 布置与 GenerateImageSplitTest 相同（继承它会把那十条用例重跑一遍，
    所以这里只借它的 setUp）。
    """

    def setUp(self):
        GenerateImageSplitTest.setUp(self)

    def _entry(self):
        return GenerateImageSplitTest._entry(self)

    def _anima(self):
        """动漫渠道的骨架：两段采样，两个 KSampler、两处 `__SEED__`。"""
        return {"2": {"class_type": "KSampler",
                      "inputs": {"seed": "__SEED__", "steps": 10}},
                "27": {"class_type": "KSampler",
                       "inputs": {"seed": "__SEED__", "steps": 5}}}

    def _submit(self, prompt="a cat", **kw):
        generate_image.load_skill.return_value = {
            "workflow": kw.pop("workflow", self._anima())}
        with mock.patch.object(qq_api, "current_context",
                               return_value=("group", "9")):
            return generate_image.tool["function"](prompt, **kw)

    def _job(self):
        self.assertEqual(len(image_jobs._queue), 1)
        return image_jobs._queue[0]

    def test_pinned_seed_lands_in_both_samplers(self):
        """两个采样器共用**同一个数**，不是两个种子。

        占位符是全局字符串替换（见 generate_image 里那段 ⚠️ 注释），所以「这张图
        的种子」始终就是 caption 上报出去的那一个：把它填回 ComfyUI 的两个
        KSampler 就能复现。别看到「双采样器」就以为要报两个号。
        """
        self._submit("a cat", seed=42)
        job = self._job()
        self.assertEqual(job.workflow["2"]["inputs"]["seed"], 42)
        self.assertEqual(job.workflow["27"]["inputs"]["seed"], 42)

    def test_no_placeholder_survives_the_substitution(self):
        """漏一处 `__SEED__` 就等于这张没按种子跑（ComfyUI 提交会失败）。"""
        self._submit("a cat", seed=42)
        self.assertNotIn("__SEED__", json.dumps(self._job().workflow))

    def test_recorded_seed_is_the_one_that_ran(self):
        """caption 和账本用的是 Job.seed——它必须和工作流里那个数相同。

        两处分开算的话（比如发图时再随机一次），对方抄到的种子就跟这张图无关了。
        """
        self._submit("a cat", seed=42)
        self.assertEqual(self._job().seed, 42)

    def test_omitted_seed_is_random_but_still_recorded(self):
        """不点名种子时系统随机——随机出来的那个照样要记着，否则这张的种子当场
        就丢了，下次想复现无从查起。"""
        self._submit("a cat")
        job = self._job()
        self.assertTrue(1 <= job.seed <= generate_image.SEED_MAX)
        self.assertEqual(job.workflow["2"]["inputs"]["seed"], job.seed)

    def test_empty_string_seed_counts_as_not_given(self):
        """模型常把没填的参数塞成空串：它该等同于「没传」（随机），而不是报错。"""
        self._submit("a cat", seed="")
        self.assertTrue(1 <= self._job().seed <= generate_image.SEED_MAX)

    def test_seed_zero_is_a_real_seed(self):
        """0 是合法种子，不是「没传」。用 `if seed:` 判会把这张的种子弄丢。"""
        self._submit("a cat", seed=0)
        self.assertEqual(self._job().seed, 0)

    def test_digit_string_is_accepted(self):
        """`seed: "1234567890"` 和 `seed: 1234567890` 都常见：execute_tool 是
        fn(**args)，不做类型清洗。"""
        self._submit("a cat", seed="4100493889")
        self.assertEqual(self._job().seed, 4100493889)

    def _refused(self, raw, *why):
        out = self._submit("a cat", seed=raw)
        self.assertEqual(image_jobs.queue_depth(), 0)    # 一张都没进队
        for w in why:
            self.assertIn(w, out)
        return out

    def test_out_of_range_is_refused_not_clamped(self):
        """钳到范围内 = 画一张对不上的图，对方还以为自己记错了号。

        宁可一句错话让他回头再确认一次——见 generate_image._resolve_seed。
        """
        self._refused(generate_image.SEED_MAX + 1, "超出范围")
        self._refused(-1, "超出范围")

    def test_non_numeric_is_refused(self):
        self._refused("abc", "不是一个数字")

    def test_bool_is_refused(self):
        """bool 是 int 的子类：不挡就会把 True 当成种子 1 悄悄画一张。"""
        self._refused(True, "不能是 true/false")

    def test_float_is_refused(self):
        self._refused(1.5, "整数")

    def test_bad_seed_bails_before_reading_the_skill(self):
        """种子在动手之前就定下来：一个抄错的号不该先把源图上传、工作流读满
        再报错（load_skill、垫图上传、探活都在它后面，见 _generate_image 顺序）。"""
        self._refused("abc", "不是一个数字")
        generate_image.load_skill.assert_not_called()

    def test_nai_refuses_a_seed_out_loud(self):
        """NAI 是云端出图，同一个数在它那边不保证复现（用户 2026-10-02：本机
        才有种子的意义）。但**必须明说**——默默忽略的话，对方会以为「同种子
        换提示词」在 NAI 上也成立，照着做就对不上图。"""
        out = self._submit("a cat", skill="nai", seed=42)
        self.assertIn("NAI", out)
        self.assertIn("种子", out)
        self.assertEqual(image_jobs.queue_depth(), 0)

    def test_pinned_seed_splits_the_duplicate_check(self):
        """点名的种子进意图指纹：同种子同提示词提交两遍算重复（那张就在队里），
        换个种子则是「真要两张」，照排。"""
        with mock.patch.object(image_jobs, "wait_done",
                               return_value={"outputs": {}}), \
                mock.patch.object(qq_api, "current_context",
                                  return_value=("group", "9")):
            generate_image.load_skill.return_value = {"workflow": self._anima()}
            generate_image.tool["function"]("a cat", seed=1)
            same = generate_image.tool["function"]("a cat", seed=1)
            other = generate_image.tool["function"]("a cat", seed=2)
        self.assertIn("没有执行", same)
        self.assertNotIn("没有执行", other)
        self.assertEqual(image_jobs.inflight_count("group", "9"), 2)

    def test_random_seed_does_not_split_the_duplicate_check(self):
        """**没点名时的随机数绝不进指纹**：否则模型两次「画一只猫」会被算成两个
        意图，查重直接失效——那正是 2026-10-01 重复出图的老路。"""
        with mock.patch.object(image_jobs, "wait_done",
                               return_value={"outputs": {}}), \
                mock.patch.object(qq_api, "current_context",
                                  return_value=("group", "9")):
            generate_image.load_skill.return_value = {"workflow": self._anima()}
            generate_image.tool["function"]("a cat")
            out = generate_image.tool["function"]("a cat")
        self.assertIn("没有执行", out)
        self.assertEqual(image_jobs.inflight_count("group", "9"), 1)

    def test_web_receipt_names_the_seed_it_ran(self):
        """网页端同步拿结果，回执里那个数就是这张图跑的种子。"""
        generate_image.load_skill.return_value = {"workflow": self._anima()}
        with mock.patch.object(qq_api, "current_context",
                               return_value=(None, None)), \
                mock.patch.object(image_jobs, "wait_done",
                                  return_value=self._entry()):
            out = generate_image.tool["function"]("a cat", seed=42)
        self.assertIn("seed: 42", out)


class _QueueOnlyTest(unittest.TestCase):
    """只跟队列记账打交道的用例：绝不能让它顺手起真 worker。

    enqueue() 会调 _ensure_worker 起一个常驻线程，而这个线程**不会**被 _reset
    收掉（_worker_started 是进程级的，见 _reset 的注释）。真起起来它就活到进程
    结束，跑到别的模块的用例里去抢任务，症状是随机某个用例失败——比如
    ProcessTest 里 _wait_comfy_idle 被调了两次（2026-10-01 实测，就是这么踩的）。
    """

    def setUp(self):
        image_jobs._reset()
        p = mock.patch.object(image_jobs, "_ensure_worker", lambda: None)
        p.start()
        self.addCleanup(p.stop)


class PendingDuplicateTest(_QueueOnlyTest):
    """同一件事还没出图又被提交一遍 → 不再排第二张（2026-10-01）。

    为什么要拦在代码层：QQ 路径下 generate_image 提交完立刻返回，模型那一轮
    只拿到「排上了」；下一轮它要是没看到这条回执（上下文被压缩，或空头承诺
    守卫把如实汇报判成空头承诺），就会再提交一次，队列里多出一张一模一样的
    图——实测 2026-10-01 08:13 就是这么连出两张 hd_3 的。
    """

    def _enqueue(self, prompt="a cat", skill=None, lora=None,
                 target="group", target_id="9"):
        intent = generate_image._intent_key(prompt, skill, lora)
        job, reason = image_jobs.enqueue(target, target_id, {"1": {}}, skill,
                                         prompt=prompt, intent=intent)
        self.assertIsNone(reason)
        return job, intent

    def test_the_same_intent_in_flight_is_a_duplicate(self):
        job, intent = self._enqueue()
        self.assertIs(image_jobs.find_pending_duplicate("group", "9", intent),
                      job)

    def test_a_different_prompt_is_not_a_duplicate(self):
        self._enqueue(prompt="a cat")
        other = generate_image._intent_key("a dog", None, None)
        self.assertIsNone(image_jobs.find_pending_duplicate("group", "9", other))

    def test_a_different_channel_is_not_a_duplicate(self):
        """换渠道是另一张图——hd_2 和 hd_3 不能互相顶掉。"""
        self._enqueue(skill=None)
        other = generate_image._intent_key("a cat", "hd_3", None)
        self.assertIsNone(image_jobs.find_pending_duplicate("group", "9", other))

    def test_another_sessions_intent_does_not_collide(self):
        """别的会话排着一张同样的图，跟这个会话没关系。"""
        _, intent = self._enqueue(target="group", target_id="8")
        self.assertIsNone(image_jobs.find_pending_duplicate("group", "9", intent))
        self.assertIsNotNone(
            image_jobs.find_pending_duplicate("group", "8", intent))

    def test_a_job_without_intent_never_matches(self):
        image_jobs.enqueue("group", "9", {"1": {}})
        intent = generate_image._intent_key("a cat", None, None)
        self.assertIsNone(image_jobs.find_pending_duplicate("group", "9", intent))

    def test_a_running_job_is_still_in_flight(self):
        """已经开跑但还没出图，照样算重复——对方什么都还没看到。"""
        job, intent = self._enqueue()
        image_jobs._take_nowait()
        self.assertIs(image_jobs.find_pending_duplicate("group", "9", intent),
                      job)

    def test_a_landed_image_is_no_longer_a_duplicate(self):
        """出过图之后再点一次同样的是有意为之。

        实测 2026-10-01 08:18 有人明说「三档同一份词条原样重跑一张 你看看
        这次细节对不对」——这种当重复拦下等于把正常需求堵死。
        """
        job, intent = self._enqueue()
        image_jobs._take_nowait()
        image_jobs._finish(job)
        self.assertIsNone(image_jobs.find_pending_duplicate("group", "9", intent))


class RecentActivityTest(_QueueOnlyTest):
    """守卫开火前问的那句「本会话有没有在途 / 刚出图」（2026-10-01）。

    没有它，「上一轮提交、这一轮汇报进度」会被判成空头承诺退回重来，而那段
    nudge 会让模型真的再提交一遍——正是重复出图的老路（见
    image_jobs.recent_activity 的注释）。
    """

    def test_a_queued_job_counts(self):
        image_jobs.enqueue("group", "9", {"1": {}})
        self.assertEqual(image_jobs.recent_activity("group", "9"), 1)

    def test_a_running_job_counts(self):
        image_jobs.enqueue("group", "9", {"1": {}})
        image_jobs._take_nowait()
        self.assertEqual(image_jobs.recent_activity("group", "9"), 1)

    def test_a_just_landed_image_counts(self):
        job, _ = image_jobs.enqueue("group", "9", {"1": {}})
        image_jobs._take_nowait()
        image_jobs._finish(job)
        self.assertEqual(image_jobs.recent_activity("group", "9"), 1)

    def test_a_long_gone_image_does_not_count(self):
        job, _ = image_jobs.enqueue("group", "9", {"1": {}})
        image_jobs._take_nowait()
        image_jobs._finish(job)
        # within 取负 = 窗口外。用 0 不行：Windows 的时钟精度会让 ts 和 now
        # 读到同一刻，`now - ts <= 0` 照样成立。
        self.assertEqual(image_jobs.recent_activity("group", "9", within=-1), 0)

    def test_another_session_does_not_count(self):
        image_jobs.enqueue("group", "8", {"1": {}})
        self.assertEqual(image_jobs.recent_activity("group", "9"), 0)

    def test_no_target_is_zero(self):
        self.assertEqual(image_jobs.recent_activity(None, None), 0)


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
        self.assertIn("图画好了", text)
        self.assertNotIn("没在线", text)

    def test_send_stage_never_claims_the_image_was_not_drawn(self):
        """图都画好了，投递失败时**绝不能说「图没画出来」**（2026-10-01 03:44）。

        现场：Anima_00276_.png 明明出图了（3.89MB），胡桃桃收到的却是
        「图没画出来（OneBot 调用失败 send_private_msg: ）」——话术把人引向
        「重画一次」这个完全没用的方向。根因在 qq_api 漏认措辞，但谎话是
        这里说的，所以在这儿钉住。
        """
        for exc in (OSError("down"),
                    RuntimeError("OneBot 调用失败 send_private_msg: ")):
            text = image_jobs._fail_text(exc, stage="send")
            self.assertNotIn("图没画出来", text)
            self.assertIn("图画好了", text)

    def test_send_stage_names_the_friend_problem_when_that_is_the_cause(self):
        """非好友发不出去 → 直接说「加好友」，别甩半截 OneBot 报文。"""
        exc = RuntimeError(
            "OneBot 调用失败 send_private_msg: {'status': 'failed', "
            "'retcode': 100, 'data': None, 'wording': 'OIDB error 170019003 "
            "on 0x11c5_100: verify identify fail'}")
        text = image_jobs._fail_text(exc, stage="send")
        self.assertIn("加好友", text)
        self.assertNotIn("OneBot", text)

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

    def test_comfy_crash_is_told_apart_from_slowness(self):
        """**ComfyUI 中途崩了 ≠ 画得慢**——这是 09-30 那个坑的锁。

        现场（user/comfyui_8188.prev.log 01:19:41）：ComfyUI 原生崩溃
        （faulthandler 只有 C 栈、没有 Python 帧 = SIGSEGV 一类硬崩）。可当时
        那张图等满 180 秒后报的是「画超时了……麻烦重新生成一次」——把「服务没了」
        说成「这次慢」，用户会一次次白重试。所以现在等图期间会再探一次
        /system_stats：探不到就改判 `ComfyGone`。
        """
        text = image_jobs._fail_text(image_jobs.ComfyGone())
        self.assertIn("掉线", text)
        self.assertNotIn("重新生成一次", text)
        self.assertNotIn("超时", text)

    def test_gone_message_points_at_recovery_not_retry(self):
        """话术要告诉人「等它自己起来」，而不是「你再画一次」。"""
        text = image_jobs._fail_text(image_jobs.ComfyGone())
        self.assertIn("重新起来", text)


class ComfyGoneTest(_Base):
    """等图期间 ComfyUI 没了 → 说「掉线」，不要说「画得慢」。

    单测 `_fail_text(ComfyGone())` 只证明话术对；这里证明**接线**也对：
    `_drain` 走到超时分支时真的会去探活，探不到就换成 `ComfyGone`。
    """

    def _timeout_run(self, comfy_alive):
        """跑一张必然超时的图；comfy_alive = 超时后探活的结果。"""
        p = mock.patch.object(image_jobs, "_comfy_up", lambda timeout=3: comfy_alive)
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(image_jobs, "wait_done",
                               side_effect=TimeoutError("生成超时 (180s)")):
            self._enqueue()
            image_jobs._drain()

    def test_crash_during_wait_becomes_comfy_gone(self):
        self._timeout_run(comfy_alive=False)
        self.assertEqual(len(self.sent_texts), 1)
        text = self.sent_texts[0][2]
        self.assertIn("掉线", text)
        self.assertNotIn("重新生成一次", text)

    def test_slow_render_keeps_the_timeout_wording(self):
        """ComfyUI 还在、只是慢：照旧说「超时，重新生成一次」。"""
        self._timeout_run(comfy_alive=True)
        text = self.sent_texts[0][2]
        self.assertIn("超时", text)
        self.assertNotIn("掉线", text)


class StuckComfyRestartTest(_Base):
    """超时中断后 ComfyUI「活着但卡住」→ 重启它，别让后面每一张陪葬。

    2026-10-03 加。为什么必须重启：`/interrupt` 设的是一个**协作**标志，节点
    跑完一步才去读它。卡在一次不返回的 CUDA 调用里（TDR / 僵尸）时，这个标志
    永远读不到 → `queue_running` 永远不清空 → 后面每一张都撞上同一个忙队列，
    一张接一张地超时（09-30 的级联）。这种状态不会自己好。

    判据是「探得到 + 等满 COMFY_IDLE_WAIT 还没退场」。⚠️ 真挂了（探不到）
    **不能**重启——`_restart_comfy` 会白等满 COMFY_RESTART_WAIT(180s) 才放弃，
    而下一张本来也只会撞上「ComfyUI 没在线」。
    """

    def _timeout_run(self, idle, alive):
        """跑一张必然超时的图；idle/alive = 清理与探活的结果。"""
        self.restarts = []
        for target, repl in (
                ("_wait_comfy_idle", lambda timeout=90: idle),
                ("_comfy_up", lambda timeout=3: alive),
                ("_restart_comfy", lambda *a, **k: self.restarts.append(1) or True)):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(image_jobs, "wait_done",
                               side_effect=TimeoutError("生成超时 (180s)")):
            self._enqueue()
            image_jobs._drain()

    def test_alive_but_stuck_triggers_restart(self):
        """探得到却退不了场 = 卡住 → 重启一次；话术仍按「超时」说。"""
        self._timeout_run(idle=False, alive=True)
        self.assertEqual(len(self.restarts), 1)
        text = self.sent_texts[0][2]
        self.assertIn("超时", text)
        self.assertNotIn("掉线", text)

    def test_dead_comfy_is_not_restarted(self):
        """真挂了不重启：白等 180 秒没意义，说「掉线」比说「重试」诚实。"""
        self._timeout_run(idle=False, alive=False)
        self.assertEqual(self.restarts, [])
        self.assertIn("掉线", self.sent_texts[0][2])

    def test_normal_cleanup_never_restarts(self):
        """正常退场（多数情况）一张都不该重启——重启要丢热的模型。"""
        self._timeout_run(idle=True, alive=True)
        self.assertEqual(self.restarts, [])


class ChannelSwitchTest(_Base):
    """换渠道先 /free：同渠道连画保持模型热，跨渠道才释放。

    背景（2026-09-27 实测）：12GB 显存 + 16GB 内存撑不住两个渠道的模型同时
    驻留。动漫渠道（现默认 anima_clear）连跑两张都正常，紧接着同一个 ComfyUI 会话里跑 qwen
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
        self._run(skill="anima_soft")
        self.assertEqual(self.events, ["submit"])

    def test_same_skill_twice_never_frees(self):
        """同渠道连画必须保持模型热的——否则每次都白等一次重新加载。"""
        self._run(skill="anima_soft")
        self._run(skill="anima_soft")
        self.assertEqual(self.events, ["submit", "submit"])

    def test_switch_frees_before_submitting(self):
        """顺序要紧：先 free 再 submit，否则新任务还是和旧模型抢显存。"""
        self._run(skill="anima_soft")
        self.events.clear()
        self._run(skill="qwen_image_v1")
        self.assertEqual(self.events, ["free", "submit"])

    def test_switching_back_also_frees(self):
        """来回切也算切换，两个方向都要释放。"""
        self._run(skill="anima_soft")
        self._run(skill="qwen_image_v1")
        self.events.clear()
        self._run(skill="anima_soft")
        self.assertEqual(self.events, ["free", "submit"])

    def test_no_skill_keeps_old_behaviour(self):
        """老调用方不传 skill：绝不能因此多打 /free，行为要和从前一样。"""
        self._run()
        self._run()
        self.assertEqual(self.events, ["submit", "submit"])

    def test_skill_none_after_real_skill_is_not_a_switch(self):
        """传 None 不是「换渠道」——不能拿 None 去和 anima 比出一次切换。"""
        self._run(skill="anima_soft")
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
        # 水位必须钉死：默认值会被 .env 覆盖（本机 COMFY_MIN_FREE_RAM_GB=2.0），
        # 于是用例里的 1.5GB 不再「低于水位」，重启永远不触发——判定逻辑没坏，
        # 是这条用例在跟着环境走。钉成 3.0，跟 .env 无关。
        self._threshold(3.0)
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
        image_jobs._last_skill = "anima_soft"
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
    证据，残留 1.6~2.1GB 会一路叠上去。动漫渠道（现默认 anima_clear，峰值约 5.4GB）扛得住，但 qwen
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
        """动漫渠道跑完还剩约 5.5GB——不该动它，模型保持热的。"""
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

    这是**机制**用例：每个都显式把某个渠道塞进 `DISABLED_IMAGE_SKILLS` 再验，
    所以跟「现在有没有渠道真被停用」无关。

    为什么需要这道闸：光靠「不进 skills 白名单」挡不住——白名单只管提示词里
    列不列，模型记得这个名字照样能把 skill 传进来。停用必须落在代码里。

    ⚠️ 2026-10-01 用户拍板**全部解封**，`DISABLED_IMAGE_SKILLS` 已清空
    （qwen_image_v1 与 krea2 都放开）。所以本类里的 krea2 只是**举例用的名字**，
    不代表它真的停用——真配置的状态由 `test_real_config_disables_nothing` 钉。
    """

    def setUp(self):
        image_jobs._reset()
        self.comfy = mock.Mock(return_value=True)
        self.load = mock.Mock(return_value={"workflow": {"1": {}}})
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

    def test_named_krea2_is_refused(self):
        self._disabled(["krea2"])
        out = self._call(prompt="a cat", skill="krea2")
        self.assertIn("停用", out)
        self.assertFalse(self.comfy.called)     # 一步都没碰 ComfyUI
        self.assertFalse(self.load.called)      # 连 skill 都没去读

    def test_i2i_without_naming_a_channel_uses_the_default(self):
        """只给 source_image 不给 skill：落到默认渠道（anima_clear），它支持垫图。

        这条以前是「必须挡住」（那时图生图整体停用）。现在反过来：模型说「改图」
        而不点名渠道，就该按默认渠道垫图，而不是被拒。
        顺带钉住源图的缩放目标——必须按**本档画布的长边**缩，不是 comfy_src
        默认的 1216。图生图的出图尺寸就是这一步缩出来的尺寸再乘二段放大倍率，
        缩错了高清档就名不副实。
        """
        self._disabled([])
        self.load.return_value = {
            "workflow": {"9": {"class_type": "EmptyLatentImage",
                               "inputs": {"width": 728, "height": 1024}}},
            "path": os.path.join("skills", "anima_clear")}
        seen = {}

        def fake_fit(raw, max_side=None):
            seen["max_side"] = max_side
            return b"FIT", (728, 1024)

        with mock.patch.object(generate_image, "load_workflow",
                               lambda p: {"30": {"class_type": "LoadImage"}}), \
                mock.patch.object(generate_image.comfy_src, "resolve",
                                  lambda spec: (b"RAW", "引用的那张图")), \
                mock.patch.object(generate_image.comfy_src, "fit", fake_fit), \
                mock.patch.object(generate_image.comfy_src, "upload",
                                  lambda raw: "i2isrc_x.png"):
            out = self._call(prompt="把衣服换成红色", source_image="1")

        self.assertIn("已经在画了", out)
        self.assertEqual(seen["max_side"], 1024)     # 728×1024 画布的长边
        self.assertTrue(self.comfy.called)           # 走到底了：探活过 ComfyUI

    def test_i2i_is_refused_on_hd_3(self):
        """三档不给图生图：点名也一样拒，而且一步都不碰 ComfyUI。

        「不给」跟图生图的开销无关（实测只比文生图多 3~6 秒），是三档自己贵。
        """
        self._disabled([])
        out = self._call(prompt="把衣服换成红色", skill="hd_3_clear",
                         source_image="1")
        self.assertIn("不支持图生图", out)
        self.assertFalse(self.comfy.called)

    def test_refusal_tells_the_model_what_to_do(self):
        """拒收不能只说「不行」——模型得知道下一步该干嘛，否则它会开始编。"""
        self._disabled(["krea2"])
        out = self._call(prompt="a cat", skill="krea2")
        # 给出可用的替代——用常量而不是字面量，免得换默认渠道时又漏一条
        from app.tools.normal.generate_image import T2I_DEFAULT_SKILL
        self.assertIn(T2I_DEFAULT_SKILL, out)
        self.assertIn("别跟对方提", out)          # 别把渠道名甩给用户

    def test_other_channels_are_untouched(self):
        """闸只挡停用的那个，别的渠道照常走。"""
        self._disabled(["krea2"])
        out = self._call(prompt="a cat", skill="anima_soft")
        self.assertNotIn("停用", out)
        self.assertTrue(self.comfy.called)      # 正常路径照旧会探活

    def test_empty_list_re_enables_it(self):
        """开关清空就恢复——证明这是配置项，不是写死的判断。"""
        self._disabled([])
        out = self._call(prompt="a cat", skill="krea2")
        self.assertNotIn("停用", out)
        self.assertTrue(self.comfy.called)

    def test_real_config_disables_nothing(self):
        """真配置里**没有任何渠道被停用**——2026-10-01 用户拍板全部解封。

        这条以前钉的是「qwen + krea2 必须停用」。用户解封后反过来钉：列表必须
        是空的，免得哪天有人（或某个残留的 .env）又悄悄把渠道关掉、让用户点名的
        渠道莫名其妙画不出来。
        """
        from app.config import DISABLED_IMAGE_SKILLS
        self.assertEqual(list(DISABLED_IMAGE_SKILLS), [])

    def test_real_config_keeps_the_runnable_channels(self):
        """四个动漫渠道是**能跑**的渠道，绝不能被误列进停用清单
        （那样就一张图都画不出了）。"""
        from app.config import DISABLED_IMAGE_SKILLS
        for name in ("anima_soft", "anima_gloss", "anima_curvy", "anima_clear"):
            self.assertNotIn(name, DISABLED_IMAGE_SKILLS, name)


class QqWhitelistTest(unittest.TestCase):
    """QQ 白名单要**精确**：只放它该用的渠道，别的一律看不见。

    白名单不列出 = 模型想不起来，这比「调用时被拒」更早一层；两件事都要做
    —— 09-27 已经吃过一次（光靠白名单挡不住模型传参，所以停用还得有代码闸，
    见 DisabledChannelTest）。

    ⚠️ 2026-10-01 起 `DISABLED_IMAGE_SKILLS` 已清空，所以这里测的**不再是
    「停用渠道」**，而是「QQ 本身提不提供哪些渠道」。同日用户拍板把
    `image_gen_v1`（SD）与 `krea2` 也放给 QQ——它们以前只在 `agents/draw` 里。
    2026-10-02 又放了新上的 `nffa`。
    现在 QQ 侧的隐藏项只剩**已归档的老渠道**和**画图助手专用 / 未上线**的那些。
    """

    # QQ 提供的：16 个动漫渠道（4 画风 × 4 尺寸档）+ qwen + SD + krea2 + nffa
    VISIBLE = tuple("%s_%s" % (tier, style)
                    for tier in ("anima", "hd_fast", "hd_2", "hd_3")
                    for style in ("clear", "soft", "gloss", "curvy")) + (
                        "qwen_image_v1", "image_gen_v1", "krea2", "nffa")
    # QQ 不提供的：已归档的老名字 + draw 专用 / 还没上线的渠道
    HIDDEN = ("anima", "anima_2", "anima_realskin",
              "image_gen_v1_hires", "nsfw_pose_gen", "pose_library")

    def test_qq_whitelist_matches_the_offered_channels(self):
        from app import agents
        for name in self.VISIBLE:
            self.assertTrue(agents.allows_skill("qq", name), name)
        for name in self.HIDDEN:
            self.assertFalse(agents.allows_skill("qq", name), name)

    def test_qq_skill_list_only_advertises_the_offered_channels(self):
        from app.agent_prompt import _build_skill_list
        block = _build_skill_list("qq")
        for name in self.VISIBLE:
            self.assertIn("**" + name + "**", block, name)
        for name in self.HIDDEN:
            self.assertNotIn("**" + name + "**", block, name)

    def test_description_no_longer_calls_krea2_disabled(self):
        """krea2 解封后，描述里不能再写「已停用 / 不要传」这种话——
        它现在是**备选渠道**，只在用户点名时才用。"""
        from app.tools.normal.generate_image import tool
        desc = tool["description"]
        self.assertNotIn("已停用", desc)
        self.assertNotIn("不要传 skill=krea2", desc)
        self.assertIn("krea2", desc)
        # qwen 依旧可用，尺寸也还写着（2026-10-03 起文生图是 1024×1536）
        self.assertIn("qwen_image_v1", desc)
        self.assertIn("1024×1536 竖版", desc)

    def test_description_teaches_when_to_use_nffa(self):
        """新渠道必须**带着用法**进描述（2026-10-02 的规矩：加能力不写模型
        看得见的用法 = 白加）。这里钉三件最容易说错的事：只在点名时用、
        提示词全自己写（不拼画风前缀）、不支持垫图。"""
        from app.tools.normal.generate_image import tool
        desc = tool["description"]
        self.assertIn("skill=nffa", desc)
        self.assertIn("1024×1536", desc)
        self.assertIn("不拼任何画风前缀", desc)
        self.assertIn("不支持垫图", desc)
        # 参数枚举里也得有它，否则模型传值会被当成非法
        self.assertIn("nffa", tool["parameters"]["properties"]["skill"]["description"])


class CleanStartTest(_Base):
    """「开跑前先要一个干净的 ComfyUI」（2026-09-27 加，见
    image_jobs.CLEAN_START_SKILLS）。

    ## 2026-09-30：这张清单被清空了，这些用例改成守「它必须保持惰性」

    原先 `CLEAN_START_SKILLS = {"anima_2": 6.0}`——理由是「两段采样要摊开
    5.4GB，而脏状态只剩 5.5GB，余量太薄」。**这个理由站不住脚**：

      * 09-30 的 comfyui_8188.log 里，那套两段（10+5）工作流在**同一个脏进程**
        上连跑 8 次全成（10.7~11.7 秒），最密两张只隔 1 秒——ComfyUI 自己的
        DynamicVRAM 会在两段之间换出不需要的权重，峰值并没有真叠到 5.4GB。
      * 真正崩过的那次（01:57:30 CUDA error）跑的是**旧的单段 15 步**，跟两段
        采样没有因果关系。
      * 代价却是实的：这个门槛在一天里白重启了 **23 次** ComfyUI。

    而它现在更危险——两段那套已经是**全部四个动漫渠道**（默认 `anima_clear`）。
    要是把 6.0 顺手挪到它们头上，每张默认图都要先重启一次（60~90 秒），
    比原来的 bug 更糟。

    所以下面测的是**反过来的性质**：机制留着（改 `{}` 即可重现），但对包括
    `anima_soft` 在内的任何渠道都不再触发重启。
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

    def test_no_channel_is_registered_for_a_clean_start(self):
        """**核心不变量**：清单必须是空的。

        页面上写 `{"anima_soft": 6.0}` 这类「为两段采样预备重启」的配置一律判红——
        那会让默认渠道每张图都白等一分钟。
        """
        self.assertEqual(image_jobs.CLEAN_START_SKILLS, {})

    def test_default_channel_never_restarts_even_on_a_dirty_state(self):
        """anima_soft 撞上脏状态（5.6GB）也不重启——它就是两段采样那条，实测够用。"""
        self._run("anima_soft", 5.6)
        self.assertEqual(self.restarts, [])
        self.assertEqual(len(self.sent_images), 1)

    def test_default_channel_never_restarts_on_a_critical_state(self):
        """哪怕显存低到 0.5GB 也不为它重启——低水位由 `/free` 那道闸管
        （COMFY_MIN_FREE_VRAM_GB），跟「预备重启」不是一回事。"""
        self._run("anima_soft", 0.5)
        self.assertEqual(self.restarts, [])

    def test_unknown_vram_does_not_restart(self):
        """显存问不到就别折腾：那种情况 ComfyUI 多半已经不在了，重启请求
        同样发不出去——照常提交，让 _notice 去说「ComfyUI 没在线」。"""
        self._run("anima_soft", None)
        self.assertEqual(self.restarts, [])
        self.assertEqual(len(self.sent_images), 1)


class NaiCloudTest(_Base):
    """NAI 云端分支：完全不碰 ComfyUI，图由 NovelAI 出，worker 发回原群。"""

    def setUp(self):
        super().setUp()
        # 审核开关读的是 settings.json（热路径），拨到临时目录：否则本机
        # agents/qq/settings.json 里 image_audit_groups=true 会让这些用例
        # 走真审核，假路径读图失败 → fail-closed 拦下 → 断言全崩。
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(agents, "AGENTS_DIR",
                              os.path.join(self.tmp.name, "agents"))
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
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

    def test_nai_blocked_by_audit_sends_nothing(self):
        """审核拦下 NAI 的图：不发图、不报错（提示由 image_audit 自己回）。"""
        from app import image_audit
        with mock.patch.object(image_audit, "allow_send", return_value=False):
            self._enqueue(("group", "9"), wf="x", skill="nai")
            image_jobs._drain()
        self.assertEqual(len(self.nai_sent), 0)
        self.assertEqual(self.sent_texts, [])


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
        # 2026-09-30：`description_overrides` 删了（角色底模机制下线），
        # 两端共用同一份 description，所以这里只验它本身。
        self.assertIsInstance(generate_image.tool["description"], str)
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
            self._enqueue(skill="anima_soft")
            image_jobs._drain()
        line = image_jobs.recent_line("group", "9")
        self.assertIn("已完成：", line)
        self.assertIn("已出图（anima_soft）", line)
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
            self._enqueue(skill="anima_soft")
            image_jobs._drain()
        line = image_jobs.recent_line("group", "9")
        self.assertIn("失败（anima_soft：超时）", line)

    def test_only_the_last_three_and_newest_is_last(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            for _ in range(4):
                self._enqueue(skill="anima_soft")
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
            self._enqueue(("group", "9"), skill="anima_soft")
            image_jobs._drain()
        self.assertNotEqual(image_jobs.recent_line("group", "9"), "")
        self.assertEqual(image_jobs.recent_line("group", "8"), "")

    def test_web_target_has_no_recall(self):
        # 网页侧同步等结果，模型直接从工具返回值就知道成没成，不需要回执
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue((None, None), skill="anima_soft")
            image_jobs._drain()
        self.assertEqual(image_jobs.recent_outcomes(None, None), [])
        self.assertEqual(image_jobs.recent_line(None, None), "")

    def test_empty_when_nothing_ran(self):
        self.assertEqual(image_jobs.recent_line("group", "9"), "")


if __name__ == "__main__":
    unittest.main()
