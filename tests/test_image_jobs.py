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


def _no_net_requests():
    """一个**永不出网**的 requests 替身。

    requests 这一层挡住，比逐条去挡调用点可靠：_abort / _report_and_free 里的
    /interrupt、/queue、/free 打的是**真机**——机器人正在出图时跑测试，会把
    那张真的掐掉、把模型卸掉。而挡函数又会连行为一起挡掉（ProcessTest 正是靠
    真跑 _abort 来断言 /interrupt 的载荷），所以挡在 requests 这一层最合适。

    /system_stats 和 /queue 给形状正确的空壳：_report_and_free 要拿它记一行
    显存/内存，_wait_comfy_idle 要判 queue_running 清空。给空 dict 的话前者
    只是少记一行日志，后者会一直判「没退场」——所以形状必须对。
    """
    def _resp(payload):
        resp = mock.Mock()
        resp.status_code = 200
        resp.raise_for_status = mock.Mock()
        resp.json = mock.Mock(return_value=payload)
        return resp

    def _get(url, *a, **k):
        url = str(url)
        if url.endswith("/system_stats"):
            return _resp({"devices": [{}], "system": {}})
        if url.endswith("/queue"):
            return _resp({"queue_running": [], "queue_pending": []})
        return _resp({})

    return mock.Mock(get=_get, post=lambda *a, **k: _resp({}))


# 会真碰 ComfyUI 的入口——测试里必须**全部**挡掉。
#
# 这份清单只留一份，是因为各写各的已经漏过两次：先漏 _wait_comfy_idle
# （ComfyUI 离线时干等 90 秒），2026-10-03 又漏 _restart_comfy——B' 把它接进
# 超时路径之后，GenerateImageSplitTest 里一个用例就把**用户的 ComfyUI 真重启
# 了**（comfyui_8188.log 里凭空多出一个 startup，跑测从 14 秒涨到 60 秒）。
_COMFY_IO_BLOCKERS = (
    ("_ensure_worker", lambda: None),
    ("_queue_prompt", lambda wf: "pid"),
    ("comfy_alive", lambda timeout=3: True),
    ("_comfy_up", lambda timeout=3: True),
    ("_wait_comfy_idle", lambda timeout=90: True),
    ("_restart_comfy", lambda *a, **k: True),
    ("_free_vram_gb", lambda: None),
)


def _block_comfy_io(case, send_image=None, send_text=None):
    """把 requests 和 _COMFY_IO_BLOCKERS 全部挡掉，并登记到 case 的 cleanup。

    send_image / send_text 由调用方传：各用例的替身不一样（有的要记账），
    而且这两个本来就该被替掉——发图/发文本会真去碰 QQ 接口。
    """
    pairs = [("requests", _no_net_requests())] + list(_COMFY_IO_BLOCKERS)
    if send_image is not None:
        pairs.append(("_send_image", send_image))
    if send_text is not None:
        pairs.append(("_send_text", send_text))
    for target, repl in pairs:
        p = mock.patch.object(image_jobs, target, repl)
        p.start()
        case.addCleanup(p.stop)


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

        # `prompt=`（2026-10-06）：只在这些图**被审核拦下**时才用得上——进人工
        # 二审队列要带着它，管理员点过审补发后账本才补得上（编号 → 提示词）。
        # 替身接住它就行，老断言看的是前六项。
        def _fake_image(target, tid, url, tag="", skill="", seed=None,
                        prompt=""):
            self.sent_images.append((target, tid, url))
            self.sent_captions.append(tag)
            self.sent_seeds.append(seed)

        def _fake_text(target, tid, text):
            self.sent_texts.append((target, tid, text))

        _block_comfy_io(self, send_image=_fake_image, send_text=_fake_text)

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
    """谁排队、谁被拒。本地就一条队，多会话一起排（NAI 另有云端通道）。"""

    def test_jobs_line_up_across_conversations(self):
        """不同会话的任务进的是同一条队——这就是「全局串行」的意思。"""
        a, _ = self._enqueue(("group", "9"))
        b, _ = self._enqueue(("group", "8"))
        c, _ = self._enqueue(("private", "123"))
        self.assertEqual(list(image_jobs._COMFY.queue), [a, b, c])
        self.assertEqual(image_jobs.queue_depth(), 3)

    def test_ahead_counts_the_running_one(self):
        a, _ = self._enqueue()
        b, _ = self._enqueue()
        self.assertEqual(image_jobs.ahead_of(a), 0)
        self.assertEqual(image_jobs.ahead_of(b), 1)
        image_jobs._take_nowait()          # 相当于 worker 开始跑 a
        self.assertEqual(image_jobs.ahead_of(b), 1)   # a 还在跑，仍在前头

    def test_nai_depth_counts_only_nai(self):
        """状态栏的数据源：nai_depth 只数 NAI 通道的在跑/在排，本地图不算。

        拆通道之前这两张是排在**同一条队**里的（老版本靠连调两次
        `_take_nowait()` 让 NAI 那张轮上）；现在它们各排各的——本地那张跑不跑
        都影响不到 NAI 的计数，这正是拆通道要的结果。
        """
        self._enqueue(("group", "9"))                    # 本地渠道
        self._enqueue(("group", "9"), wf="cat", skill="nai")
        image_jobs._take_nowait()          # 本地那张开跑
        self.assertEqual(image_jobs.nai_depth(), (0, 1))
        self.assertEqual(image_jobs.queue_depth(), 2)    # 总量是两条加起来的
        image_jobs._take_nowait(image_jobs._NAI)         # NAI 那张开跑
        self.assertEqual(image_jobs.nai_depth(), (1, 0))

    def test_per_session_limit(self):
        for _ in range(image_jobs.MAX_INFLIGHT):
            job, reason = self._enqueue()
            self.assertIsNone(reason)
            self.assertIsNotNone(job)
        job, reason = self._enqueue()
        self.assertIsNone(job)
        self.assertIn("排着", reason)

    def test_one_session_can_queue_five_in_a_row(self):
        """一个会话能连排 5 张。

        2026-10-03 用户提的：原来在途上限是 2，第 3 张起被拒收，而拒收理由里
        明写着「别跟对方提这张图，当没画过」——所以对方看到的是「连点 5 张只
        来 2 张」，连句解释都没有。

        ⚠️ 这条**故意写死 5**，不引用 `MAX_INFLIGHT`：上面那条符号化的用例在
        常量被改回 2 时一样会绿，而它抓不住的就是这种「退回旧值」的回归。想
        调这个数就把这条数字一起改——它就是需求的记录。
        """
        for i in range(5):
            job, reason = self._enqueue(wf={"1": {"p": i}})
            self.assertIsNone(reason, "第 %d 张不该被拒" % (i + 1))
            self.assertIsNotNone(job)
        job, reason = self._enqueue(wf={"1": {"p": "over"}})
        self.assertIsNone(job)
        self.assertIn("排着", reason)

    def test_global_queue_has_room_for_several_sessions(self):
        """全局队列要容得下好几个会话各排满——一个会话最多占 1/4。

        5（MAX_INFLIGHT）× 4 = 20（MAX_QUEUE）。这条守的是 2026-10-03 那个
        耦合：把每会话上限提到 5 却留着全局 10 的话，**两个活跃群就能把队列
        占满**，第三个群一张都排不进来，直接收到「排队的人太多」。
        """
        self.assertGreaterEqual(image_jobs.MAX_QUEUE,
                                image_jobs.MAX_INFLIGHT * 4,
                                "全局队列要放得下至少 4 个排满的会话")

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


class ChannelSplitTest(_Base):
    """两条通道：本地（ComfyUI）与云端（NAI）各排各的（2026-10-03 拆）。

    拆之前 NAI 跟本地图混在同一条 FIFO 里——一张 NAI 要等前面十几张本地图
    跑完（最多十分钟），可它走的是云端、本机一帧都不渲染，纯属白等。现在它
    有自己的队列、自己的名额、自己的 worker。
    """

    def test_nai_goes_to_the_cloud_channel(self):
        """点一张 NAI：进的是云端队列，本地队列里看不见它。"""
        job, reason = self._enqueue(wf="cat", skill="nai")
        self.assertIsNone(reason)
        self.assertEqual(list(image_jobs._COMFY.queue), [])
        self.assertEqual(list(image_jobs._NAI.queue), [job])
        self.assertIs(job.chan, image_jobs._NAI)

    def test_nai_does_not_queue_behind_local_images(self):
        """本地堆着 5 张，NAI 该多快就多快。

        这是拆通道的核心收益，而且**必须从 ahead_of 上看出来**：模型是照它报
        「前面还有 N 张」的，要是还把两条队列加在一起数，它就会对一张立刻开画
        的 NAI 说「前面还有 5 张」——比不报还糟。
        """
        for i in range(5):                  # 每个会话各一张，绕开每会话上限
            job, reason = self._enqueue(("group", str(i)), skill="hd_3_clear")
            self.assertIsNone(reason)
        nai, reason = self._enqueue(("group", "9"), wf="cat", skill="nai")
        self.assertIsNone(reason)
        self.assertIsNotNone(nai)           # 少了这句，NAI 被拒时下面两条会空过
        self.assertEqual(image_jobs.ahead_of(nai), 0)
        self.assertEqual(image_jobs._take_nowait(image_jobs._NAI), nai)

    def test_limits_are_per_channel(self):
        """本地名额满了，NAI 照样进得来——两条队各算各的。"""
        for _ in range(image_jobs.MAX_INFLIGHT):
            self._enqueue(skill="hd_3_clear")
        self.assertIsNotNone(self._enqueue(skill="hd_3_clear")[1])   # 本地已满
        job, reason = self._enqueue(wf="cat", skill="nai")
        self.assertIsNone(reason)
        self.assertIsNotNone(job)

    def test_cloud_channel_has_its_own_queue_limit(self):
        """云端队排到 NAI_MAX_QUEUE 才拒收，本地的 20 张填不满它。"""
        for i in range(image_jobs.NAI_MAX_QUEUE):
            job, reason = self._enqueue(("group", str(i)), wf="x", skill="nai")
            self.assertIsNone(reason)
            self.assertIsNotNone(job)
        _, reason = self._enqueue(("group", "999"), wf="x", skill="nai")
        self.assertTrue(reason)
        self.assertIn("太多", reason)

    def test_cloud_channel_allows_parallel_takes(self):
        """云端通道能同时有 NAI_CONCURRENCY 个任务在跑（本地恒 0/1）。

        守的是 `_take_nowait` 里「已经在跑几张」这件事：本地的串行是靠单
        worker 保证的，云端要是照抄一句 `if running: return None`，并发就悄悄
        退化回 1——不会报错，只会又排起队来。
        """
        jobs = [self._enqueue(("group", str(i)), wf="x", skill="nai")[0]
                for i in range(image_jobs.NAI_CONCURRENCY)]
        taken = [image_jobs._take_nowait(image_jobs._NAI) for _ in jobs]
        self.assertEqual(taken, jobs)
        self.assertEqual(image_jobs.nai_depth(),
                         (image_jobs.NAI_CONCURRENCY, 0))
        for j in jobs:
            image_jobs._finish(j)
        self.assertEqual(image_jobs.nai_depth(), (0, 0))

    def test_two_channels_do_not_block_each_other(self):
        """两条通道互不阻塞——本地那张在队里，云端照取不误。

        原用例是「重渠道的冷却窗只管本地、不管云端」。2026-10-04 冷却窗关掉后
        前半句不成立了，但「拆两条通道」的核心诉求（云端不排本地那条队）还在，
        改成直接验证它，别被一起改坏。
        """
        local, _ = self._enqueue(("group", "1"), skill="qwen_image_v1")
        nai, _ = self._enqueue(("group", "2"), wf="cat", skill="nai")
        self.assertEqual(image_jobs._take_nowait(image_jobs._COMFY), local)
        self.assertEqual(image_jobs._take_nowait(image_jobs._NAI), nai)

    def test_inflight_counts_both_channels(self):
        """「这个人还有几张在路上」要两条一起数（私聊额度那行靠它）。"""
        self._enqueue(("private", "42"), skill="hd_3_clear")
        self._enqueue(("private", "42"), wf="cat", skill="nai")
        self.assertEqual(image_jobs.inflight_count("private", "42"), 2)

    def test_snapshot_exposes_both_queues(self):
        """状态页：老三个键还是本地通道，nai 是云端通道，depth 是合计。"""
        self._enqueue(skill="hd_3_clear")
        self._enqueue(wf="cat", skill="nai")
        self._enqueue(wf="dog", skill="nai_wide")
        s = image_jobs.snapshot()
        self.assertEqual(len(s["queued"]), 1)
        self.assertEqual([j["skill"] for j in s["queued"]], ["hd_3_clear"])
        self.assertEqual(s["nai"]["depth"], 2)
        self.assertEqual([j["skill"] for j in s["nai"]["queued"]],
                         ["nai", "nai_wide"])
        self.assertEqual(s["nai"]["running"], [])     # 云端是列表，不是 None
        self.assertEqual(s["depth"], 3)               # 两条加起来


class WorkerSpawnTest(unittest.TestCase):
    """`_ensure_worker` 要给两条通道各起线程：本地 1 个，云端 NAI_CONCURRENCY 个。

    真起线程会跟别的用例抢时序（它们统统一 `_ensure_worker` 挡掉），所以这里
    把 `threading` 换成假的，只数它被怎么调起来——顺带验「已经起过就不再起」。
    """

    class _FakeThread:
        """假线程：start() 那一刻才记账（跟真线程一样，构造不等于跑起来）。"""

        def __init__(self, sink, target=None, args=(), daemon=None, name=""):
            self.sink, self.args, self.name = sink, args, name

        def start(self):
            self.sink.append((self.name, self.args))

    def setUp(self):
        image_jobs._reset()
        self.addCleanup(image_jobs._reset)
        self.addCleanup(setattr, image_jobs._COMFY, "worker_started", False)
        self.addCleanup(setattr, image_jobs._NAI, "worker_started", False)
        self.started = []
        fake = mock.Mock()
        fake.Thread = lambda **kw: WorkerSpawnTest._FakeThread(self.started, **kw)
        p = mock.patch.object(image_jobs, "threading", fake)
        p.start()
        self.addCleanup(p.stop)

    def test_one_thread_per_worker_slot(self):
        image_jobs._COMFY.worker_started = False
        image_jobs._NAI.worker_started = False
        image_jobs._ensure_worker()
        self.assertEqual(
            [name for name, _ in self.started],
            ["image-worker-comfy-0"] +
            ["image-worker-nai-%d" % i
             for i in range(image_jobs.NAI_CONCURRENCY)])
        # 每个线程拿到的都是自己那条通道——拿错了就是把云端任务喂给 ComfyUI
        self.assertEqual([args[0] for _, args in self.started],
                         [image_jobs._COMFY, image_jobs._NAI, image_jobs._NAI])

    def test_second_call_starts_nothing(self):
        image_jobs._COMFY.worker_started = False
        image_jobs._NAI.worker_started = False
        image_jobs._ensure_worker()
        self.started.clear()
        image_jobs._ensure_worker()
        self.assertEqual(self.started, [])


class SkillPriorityTest(unittest.TestCase):
    """渠道权重：2026-10-04 起全部是 1（qwen 那套重渠道机制已按用户要求关闭）。"""

    def test_no_channel_is_heavy_anymore(self):
        """qwen 曾是唯一的重渠道（权重 5），2026-10-04 用户拍板关掉。

        原委：他连着跑漫画加字（连环 qwen i2i），被「qwen 永远排最后 + 跑完还
        隔 90 秒」卡成每张等 2.5~3 分钟。现在它与普通渠道同权、按入队顺序排。
        恢复路径见 `config.QWEN_COOLDOWN` 的注释（**三处要一起改**）。
        """
        self.assertEqual(skills.skill_priority("qwen_image_v1"), 1)

    def test_default_and_unknown_are_normal(self):
        """默认渠道、拼错的名字、写作类 skill —— 全都按普通活处理。

        「不认识就当普通活」是刻意的：权重写错方向（把普通渠道当成重的）
        会让它被无谓地延后，而延后一次就是让对方多等一张图的时间。
        """
        for name in ("hd_3_soft", "hd_3_gloss", "krea2", "image_gen_v1",
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
            self.assertEqual(skills.skill_priority("qwen_image_v1"), 1)

    def test_qwen_skill_md_no_longer_declares_priority(self):
        """真文件里那份 frontmatter 也必须**没有** priority——光清代码没用。

        2026-10-04 关机制时就是这么踩的：权重不止写在 `_SKILL_PRIORITY` 里，
        `skills/qwen_image_v1/SKILL.md` 的 frontmatter 也写了一份，而且
        frontmatter 优先（见 `skill_priority` 的实现）。只删代码那张表的话，
        机制会从 SKILL.md 里悄悄复活。
        """
        self.assertEqual(skills.skill_priority("qwen_image_v1"), 1)
        data = skills.load_skill("qwen_image_v1")
        self.assertIsNotNone(data)
        self.assertNotIn("priority",
                         skills._parse_frontmatter(data["skill_md"]),
                         "SKILL.md 的 frontmatter 又声明了 priority")


class QueuePriorityTest(_Base):
    """队列是纯先进先出——2026-10-04 起不再有「重渠道排最后」。

    原设计：qwen 权重 5，任何普通渠道（权重 1）都能插到它前面，跑完还要空一个
    冷却窗——防的是 qwen 连跑第二张 TDR（一套权重 10.5GB / 空闲 10.78GB，
    第 1 张必成、第 2 张必死）。用户 2026-10-04 拍板关掉，现在谁先入队谁先跑。
    """

    def test_qwen_keeps_its_queue_position(self):
        """核心：qwen 先入队就先跑，不再给后到的普通渠道让位。"""
        first, _ = self._enqueue(skill="qwen_image_v1")
        self._enqueue(skill="hd_3_soft")
        self.assertEqual(image_jobs._take_nowait(), first)

    def test_fifo_among_normal_jobs(self):
        """普通渠道之间还是先进先出——优先级不能把队列变成插队游戏。"""
        a, _ = self._enqueue(skill="hd_3_soft")
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
        self._enqueue(skill="hd_3_soft")
        heavy, _ = self._enqueue(skill="qwen_image_v1")
        self.assertEqual(image_jobs.ahead_of(heavy), 1)

    def test_no_heavy_ceiling_anymore(self):
        """qwen 不再有「队里最多 3 张」的拒收——连排 6 张全收。

        老上限 `MAX_HEAVY_IN_QUEUE`（第 4 张直接拒）正是用户连着跑漫画加字时
        被拦的原因之一：第 4 张进来时模型只能回一句「画不了」。它的判据是
        `job.weight > 1`，权重清零后自动失效。
        """
        n = image_jobs.MAX_HEAVY_IN_QUEUE + 3
        for i in range(n):
            # 换会话绕开 per-session 上限，专门顶重渠道这条
            job, reason = self._enqueue(("group", str(i)), skill="qwen_image_v1")
            self.assertIsNone(reason, "第 %d 张不该被拒" % (i + 1))
            self.assertIsNotNone(job)

    def test_normal_ceiling_is_not_affected(self):
        """重渠道的上限绝不能卡到普通渠道头上。"""
        for i in range(image_jobs.MAX_HEAVY_IN_QUEUE):
            self._enqueue(("group", "h" + str(i)), skill="qwen_image_v1")
        job, reason = self._enqueue(("group", "n"), skill="hd_3_soft")
        self.assertIsNone(reason)
        self.assertIsNotNone(job)


class NoCooldownTest(_Base):
    """qwen 跑完之后不再隔 90 秒——2026-10-04 关掉冷却窗后的行为。

    原设计见 image_jobs 的「重渠道优先度」：冷却窗是留给残留权重散掉的（qwen
    连跑第二张会 TDR）。用户 2026-10-04 拍板关掉——他连环跑漫画加字，每张被卡
    2.5~3 分钟。现在 qwen 跑完立刻能接着跑。
    """

    def _run_heavy(self, ctx=("group", "9")):
        """真跑完一张 qwen。"""
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        with mock.patch.object(image_jobs, "wait_done", return_value=entry):
            job, reason = image_jobs.enqueue(ctx[0], ctx[1], {"1": {}},
                                             "qwen_image_v1")
            self.assertIsNone(reason)
            image_jobs._drain()
        return job

    def test_qwen_after_qwen_runs_immediately(self):
        """核心：qwen 跑完，下一张 qwen 立刻能开跑，不再等 90 秒。"""
        self._run_heavy()
        second, _ = self._enqueue(skill="qwen_image_v1")
        self.assertEqual(image_jobs._take_nowait(), second)

    def test_waits_stays_zero(self):
        """不再有「让行」这回事，计数恒为 0。

        老实现里 `waits` 其实也不是「让行几次」而是「每秒被轮询几次」（见
        image_jobs 那个「已知坑」），关掉之后它连虚增的机会都没有。
        """
        self._run_heavy()
        heavy, _ = self._enqueue(skill="qwen_image_v1")
        self.assertEqual(heavy.waits, 0)
        image_jobs._take_nowait()
        self.assertEqual(heavy.waits, 0)

    def test_no_cooldown_window_opens(self):
        """跑完不再开冷却窗（`_finish` 的判据是 `job.weight > 1`，权重已是 1）。"""
        self._run_heavy()
        self.assertEqual(image_jobs._COMFY.heavy_done_at, 0.0)

    def test_no_cooldown_after_a_normal_job(self):
        """普通渠道跑完照旧不开冷却。"""
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        with mock.patch.object(image_jobs, "wait_done", return_value=entry):
            image_jobs.enqueue("group", "9", {"1": {}}, "hd_3_soft")
            image_jobs._drain()
        nxt, _ = self._enqueue(skill="qwen_image_v1")
        self.assertEqual(image_jobs._take_nowait(), nxt)

    def test_old_gate_still_works_if_re_enabled(self):
        """机制没删、只是关了：把权重与冷却一起打开，旧行为还在（可回滚）。

        这就是 `config.QWEN_COOLDOWN` 注释里那条恢复路径——顺手证明它有效，
        免得哪天真崩了才发现改回去也不管用。
        """
        self.clock = [1000.0]
        for target, value in (
            ("time", _FakeTime(lambda: self.clock[0])),
            ("QWEN_COOLDOWN", 90.0),
            ("skill_priority", lambda s: 5 if s == "qwen_image_v1" else 1),
        ):
            p = mock.patch.object(image_jobs, target, value)
            p.start()
            self.addCleanup(p.stop)
        self._run_heavy()
        second, _ = self._enqueue(skill="qwen_image_v1")
        self.assertIsNone(image_jobs._take_nowait())        # 冷却中：不许开跑
        self.clock[0] += 91
        self.assertEqual(image_jobs._take_nowait(), second)


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
        """抓 `_send_image` 发出去的 caption。

        附言那一段（`caption_note`，管理页可编辑）在下面 `CaptionNoteTest`
        单独测；这一族的用例只关心**第一行**那条信息串（编号 · 分辨率 ·
        渠道 · seed），所以这里统一把附言关掉，免得断言里塞满整段文案。
        """
        from app import image_out
        sent = []
        return mock.patch.object(image_out, "prepare_for_send",
                                 lambda f, fmt="jpg": "C:/tmp/y.jpg"), \
            mock.patch.object(qq_api, "send_image",
                              lambda target, tid, path, caption="":
                              sent.append((path, caption.splitlines()[0]
                                           if caption else ""))), sent

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
                                   "HT-20261001-074112-384", skill="hd_3_soft")
        self.assertEqual(
            sent, [("C:/tmp/y.jpg", "HT-20261001-074112-384 · 1024×1536 · hd_3_soft")])

    def test_caption_survives_missing_size(self):
        """量不出分辨率（图读不出来 / 回落到 ComfyUI URL）时只少这一项，别多出空档。"""
        from app import image_out
        p1, p2, sent = self._capture_captions()
        with p1, p2, \
             mock.patch.object(image_out, "local_size", return_value=""):
            image_jobs._send_image("group", "9", "b.png",
                                   "HT-20261001-074112-384", skill="hd_3_soft")
        self.assertEqual(sent, [("C:/tmp/y.jpg", "HT-20261001-074112-384 · hd_3_soft")])

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
                                   skill="hd_3_soft", seed=4100493889)
        self.assertEqual(
            sent, [("C:/tmp/y.jpg",
                    "HT-20261001-074112-384 · 1024×1536 · hd_3_soft "
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


class CaptionNoteTest(unittest.TestCase):
    """出图 caption 后面那段附言：管理页可编辑，三态（2026-10-06 用户拍板）。

    没设 = 内置默认；空串 = 这段不要；其余 = 用户自己写的那份。
    附言只在**有编号**时跟着走——`_caption` 没 tag 整行都不发。
    """

    def _note(self, stored):
        with mock.patch.object(agents, "caption_note", return_value=stored):
            return image_jobs._caption("HT-1", "b.png", "silver", image_out)

    def test_unset_falls_back_to_the_builtin_default(self):
        lines = self._note(None).splitlines()
        self.assertEqual(lines[0], "HT-1 · silver")
        self.assertIn("引用这张图", lines[1])

    def test_empty_string_switches_the_note_off(self):
        self.assertEqual(self._note("").splitlines(), ["HT-1 · silver"])

    def test_custom_note_is_appended_verbatim(self):
        self.assertEqual(self._note("要图喊我").splitlines(),
                         ["HT-1 · silver", "要图喊我"])

    def test_no_tag_sends_nothing_at_all(self):
        """没编号 = 整行不发，附言也不该单独刷一条。"""
        with mock.patch.object(agents, "caption_note", return_value=None):
            self.assertEqual(
                image_jobs._caption("", "b.png", "silver", image_out), "")


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
        self.assertEqual(image_jobs._COMFY.queue[-1].tag, job.tag)

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
        # 这个类从前是**自己抄一份**清单的，于是漏了两次：先漏 _wait_comfy_idle
        # （ComfyUI 离线时干等 90 秒），2026-10-03 又漏 _restart_comfy——B' 把它
        # 接进超时路径之后，test_web_reports_timeout 把用户的 ComfyUI 真重启了。
        # 改成共用 _block_comfy_io 那份清单，漏不了。
        _block_comfy_io(self, send_image=mock.Mock(), send_text=mock.Mock())

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
        self.assertIn("任务已提交", out)
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
        self.assertEqual(len(image_jobs._COMFY.queue), 1)
        return image_jobs._COMFY.queue[0]

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


class TimeoutRestartTest(_Base):
    """超时之后**只要 ComfyUI 还活着**就重启它，别让后面每一张陪葬。

    2026-10-03 加。两种成因都算数：

    ① **退不了场**：`/interrupt` 设的是一个**协作**标志，节点跑完一步才去读它。
       卡在一次不返回的 CUDA 调用里（TDR / 僵尸）时这个标志永远读不到 →
       `queue_running` 永远不清空 → 后面每一张都撞上同一个忙队列，一张接一张
       地超时（09-30 的级联）。
    ② **退场了、但这张烧满了 180 秒**：说明机器状态已经不对了。实测级联：
       03:05:09 超时（内存还有 6.3GB）之后，03:08:19 紧接着又超时（内存 0.1GB）
       ——第一张慢死，第二张接着死。

    两种情况 ComfyUI 都**不会自己好**。⚠️ 真挂了（探不到）**不能**重启——
    `_restart_comfy` 会白等满 COMFY_RESTART_WAIT(180s) 才放弃，而下一张本来也
    只会撞上「ComfyUI 没在线」。
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

    def test_stuck_after_interrupt_triggers_restart(self):
        """探得到却退不了场 = 卡住 → 重启一次；话术仍按「超时」说。"""
        self._timeout_run(idle=False, alive=True)
        self.assertEqual(len(self.restarts), 1)
        text = self.sent_texts[0][2]
        self.assertIn("超时", text)
        self.assertNotIn("掉线", text)

    def test_slow_timeout_also_restarts(self):
        """退场了、但这张烧满了 180 秒 → 机器状态已经不对，也清一次。"""
        self._timeout_run(idle=True, alive=True)
        self.assertEqual(len(self.restarts), 1)
        text = self.sent_texts[0][2]
        self.assertIn("超时", text)
        self.assertNotIn("掉线", text)

    def test_dead_comfy_is_not_restarted(self):
        """真挂了不重启：白等 180 秒没意义，说「掉线」比说「重试」诚实。"""
        self._timeout_run(idle=False, alive=False)
        self.assertEqual(self.restarts, [])
        self.assertIn("掉线", self.sent_texts[0][2])


class ChannelSwitchTest(_Base):
    """换渠道先 /free：跨渠道必释放。

    背景（2026-09-27 实测）：12GB 显存 + 16GB 内存撑不住两个渠道的模型同时
    驻留。动漫渠道（现默认 hd_3_clear）连跑两张都正常，紧接着同一个 ComfyUI 会话里跑 qwen
    （文本编码器 6GB + unet 4.5GB），采样到一半就 TDR，ComfyUI 变成僵尸。
    所以 skill 一变就先 /free 把上一个渠道的模型卸掉。
    同渠道连画原本永不释放，2026-10-05 起连画满 COMFY_RELEASE_AFTER_SAME
    张后也放一次（见 SameChannelReleaseTest）。
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
        self._run(skill="hd_3_soft")
        self.assertEqual(self.events, ["submit"])

    def test_same_skill_twice_never_frees(self):
        """同渠道连画必须保持模型热的——否则每次都白等一次重新加载。"""
        self._run(skill="hd_3_soft")
        self._run(skill="hd_3_soft")
        self.assertEqual(self.events, ["submit", "submit"])

    def test_switch_frees_before_submitting(self):
        """顺序要紧：先 free 再 submit，否则新任务还是和旧模型抢显存。"""
        self._run(skill="hd_3_soft")
        self.events.clear()
        self._run(skill="qwen_image_v1")
        self.assertEqual(self.events, ["free", "submit"])

    def test_switching_back_also_frees(self):
        """来回切也算切换，两个方向都要释放。"""
        self._run(skill="hd_3_soft")
        self._run(skill="qwen_image_v1")
        self.events.clear()
        self._run(skill="hd_3_soft")
        self.assertEqual(self.events, ["free", "submit"])

    def test_no_skill_keeps_old_behaviour(self):
        """老调用方不传 skill：绝不能因此多打 /free，行为要和从前一样。"""
        self._run()
        self._run()
        self.assertEqual(self.events, ["submit", "submit"])

    def test_skill_none_after_real_skill_is_not_a_switch(self):
        """传 None 不是「换渠道」——不能拿 None 去和 anima 比出一次切换。"""
        self._run(skill="hd_3_soft")
        self.events.clear()
        self._run()
        self.assertEqual(self.events, ["submit"])


class SameChannelReleaseTest(_Base):
    """同渠道连画满 N 张后，下一张提交前彻底重启 ComfyUI（2026-10-05）。

    背景（用户实录）：qwen 权重 10.5GB / 显存 11.94GB，ComfyUI 每张跑完
    残留 1.6~2.1GB 不清，连画第 3 张起 UNet 装不下、每步从内存搬 4.5GB、
    速度 ×8 → 卡死撞超时。残留叠进换页态后 /free 救不回来，当天先是改成
    连画 N 张打 /free，随后用户拍板升级成**直接重启**（显存+内存清零，
    每次约 60~90 秒不能出图，知情选定）。
    """

    def setUp(self):
        super().setUp()
        self.events = []

        def _restart():
            self.events.append("restart")
            return True

        def _free():
            self.events.append("free")

        def _submit(wf):
            self.events.append("submit")
            return "pid"

        for target, repl in (("_restart_comfy", _restart),
                             ("_report_and_free", _free),
                             ("_queue_prompt", _submit)):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)
        # 钉死阈值，别跟着 .env 漂（.env 优先于 config 默认值）
        p = mock.patch.object(image_jobs, "COMFY_RELEASE_AFTER_SAME", 2)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, skill):
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        with mock.patch.object(image_jobs, "wait_done", return_value=entry):
            image_jobs.enqueue("group", "9", {"1": {}}, skill)
            image_jobs._drain()

    def test_third_consecutive_submit_restarts_first(self):
        """连画第 3 张：先重启再 submit，顺序不能反。"""
        self._run("qwen_image_v1")
        self._run("qwen_image_v1")
        self.events.clear()
        self._run("qwen_image_v1")
        self.assertEqual(self.events, ["restart", "submit"])

    def test_first_two_stay_hot(self):
        """前 2 张不重启——不能退化成每张都重启。"""
        self._run("qwen_image_v1")
        self._run("qwen_image_v1")
        self.assertEqual(self.events, ["submit", "submit"])

    def test_cycle_every_third(self):
        """重启后重新计数：节奏是第 3、6、9…张冷启动。"""
        for _ in range(6):
            self._run("qwen_image_v1")
        # 6 张 = 6 次 submit + 2 次重启（第 3、6 张提交前）
        self.assertEqual(self.events,
                         ["submit", "submit", "restart", "submit",
                          "submit", "restart", "submit", "submit"])

    def test_restart_failure_falls_back_to_free(self):
        """重启被拒/没回来：退回打一发 /free，队列不能原地卡死。"""
        with mock.patch.object(image_jobs, "_restart_comfy",
                               return_value=False):
            self._run("qwen_image_v1")
            self._run("qwen_image_v1")
            self.events.clear()
            self._run("qwen_image_v1")
        self.assertEqual(self.events, ["free", "submit"])

    def test_switch_resets_counter(self):
        """中途换渠道归零：切回来之后又要连画满 N 张才放。"""
        self._run("qwen_image_v1")
        self._run("qwen_image_v1")
        self._run("hd_3_soft")          # 换渠道 free，计数归 1
        self._run("qwen_image_v1")       # 切回，计数 1
        self.events.clear()
        self._run("qwen_image_v1")       # 才第 2 张，必须保持热
        self.assertEqual(self.events, ["submit"])

    def test_zero_disables(self):
        """0 = 回到从前：同渠道连画永不释放。"""
        with mock.patch.object(image_jobs, "COMFY_RELEASE_AFTER_SAME", 0):
            for _ in range(4):
                self._run("qwen_image_v1")
        self.assertEqual(self.events, ["submit"] * 4)


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
        image_jobs._COMFY.last_skill = "hd_3_soft"
        self._patch([6.0], clock=_TickingClock())
        self.assertTrue(image_jobs._restart_comfy(timeout=120))
        self.assertIsNone(image_jobs._COMFY.last_skill)

    def _drive_worker(self, chan):
        """跑一个 worker 线程，记录它按什么顺序调了哪些步骤（跑完一张就退）。

        `_take` 第二次被调就抛 SystemExit 让线程干净退出——不然它会在
        `chan.wake.wait(1)` 上一直转。
        """
        calls = []
        job = mock.Mock()

        def _take(c):
            if calls.count("take"):
                raise SystemExit
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

        t = threading.Thread(target=image_jobs._worker, args=(chan,),
                             daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "worker 没按预期退出")
        return calls

    def test_worker_checks_ram_after_every_job(self):
        """本地 worker 必须在每张跑完之后看一眼内存。

        没有这条，_maybe_restart_for_ram 就是个没人调用的死函数——而它失效
        的方式是静默的：照常出图，只是内存一路涨到卡死。
        """
        self.assertEqual(self._drive_worker(image_jobs._COMFY),
                         ["take", "process", "finish", "ram"])

    def test_cloud_worker_never_touches_comfyui_memory(self):
        """云端通道跑完一张**不去看** ComfyUI 的内存水位。

        守的是 `_worker` 里那句 `if not chan.local: continue`：少了它，NAI
        每出一张图都会去探一次 ComfyUI 的内存，内存偏低时甚至会在一个只跑
        云端的机器上无端触发重启。
        """
        self.assertEqual(self._drive_worker(image_jobs._NAI),
                         ["take", "process", "finish"])


class ReleaseOnLowVramTest(unittest.TestCase):
    """显存低于水位就先 /free 再提交（2026-09-27 加）。

    针对的场景：ComfyUI **从不把上一个任务清干净**——日志里那句
    `Unloaded partially: 2896.25 MB freed, 1591.04 MB remains loaded` 就是
    证据，残留 1.6~2.1GB 会一路叠上去。动漫渠道（现默认 hd_3_clear，峰值约 5.4GB）扛得住，但 qwen
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

    def test_i2i_without_naming_a_channel_refuses_the_default(self):
        """只给 source_image 不给 skill：落默认渠道 silver，然后**明确拒收**。

        这条以前写的是「落到默认渠道，它支持垫图」。2026-10-06 用户把默认渠道
        换成 silver（文生图专用、没有垫图骨架），同时拍板「没有兜底……不需要
        代码去给它硬兜底」——所以这里**不许**静默换成某个动漫档：报错、把话讲
        清楚（图生图只走 qwen_image_v1 + source_image），一张都不画。
        """
        self._disabled([])
        out = self._call(prompt="把衣服换成红色", source_image="1")
        self.assertIn("不支持图生图", out)
        self.assertIn("qwen_image_v1", out)
        self.assertFalse(self.comfy.called)     # 一步都没碰 ComfyUI

    def test_i2i_source_is_scaled_to_the_channel_canvas(self):
        """点名会垫图的渠道时，源图按**本档画布的长边**缩，不是 comfy_src 的 1216。

        图生图的出图尺寸就是这一步缩出来的尺寸再乘二段放大倍率，缩错了高清档
        就名不副实。

        ⚠️ 用的 `anima_clear` 是 2026-10-07 **下架**的渠道：目录还在、`load_skill`
        照样能加载（屏蔽只挡 `list_skills()`），所以这条照旧成立——它钉的是
        「按画布缩」这个机制，不是「这个渠道还能被点名」。
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
            out = self._call(prompt="把衣服换成红色", skill="anima_clear",
                             source_image="1")

        self.assertIn("任务已提交", out)
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
        out = self._call(prompt="a cat", skill="hd_3_soft")
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
        for name in ("hd_3_soft", "hd_3_gloss", "hd_3_curvy", "hd_3_clear"):
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
    2026-10-07 动漫族缩到一档（三档）：`anima_*` / `hd_fast_*` / `hd_2_*` 这 12 个
    目录仍在盘上、`load_skill` 照样能加载（i2i 重绘骨架要用），但被
    `app/skills.ARCHIVED_SKILLS` 屏蔽——`list_skills()` 不再吐、白名单也不放。
    现在 QQ 侧的隐藏项 = **那 12 个被屏蔽的旧档** + 已归档的老名字 + 画图助手
    专用 / 未上线的那几个。
    """

    # QQ 提供的：4 个动漫渠道（4 画风 × 1 尺寸档，只剩三档）+ qwen + SD + nffa
    # + 2026-10-08 按风格 LoRA 拆出来的 krea2 系 5 条（通用 `krea2` 已下架）。
    VISIBLE = tuple("hd_3_" + style
                    for style in ("clear", "soft", "gloss", "curvy")) + (
                        "qwen_image_v1", "image_gen_v1", "nffa",
                        "krea2-yoneyama", "krea2-rella", "krea2-asianmix",
                        "krea2-anime2real", "krea2-coscandid")
    # QQ 不提供的：2026-10-07 屏蔽的 12 个旧档 + 已归档的老名字（含通用 krea2）
    # + draw 专用 / 未上线
    HIDDEN = tuple("%s_%s" % (tier, style)
                   for tier in ("anima", "hd_fast", "hd_2")
                   for style in ("clear", "soft", "gloss", "curvy")) + (
        "anima", "anima_2", "anima_realskin", "krea2",
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
        self.assertIn("1024×1536", desc)

    def test_description_teaches_when_to_use_nffa(self):
        """新渠道必须**带着用法**进描述（2026-10-02 的规矩：加能力不写模型
        看得见的用法 = 白加）。这里钉三件最容易说错的事：只在点名时用、
        提示词全自己写（不拼画风前缀）、不支持垫图。"""
        from app.tools.normal.generate_image import tool
        desc = tool["description"]
        self.assertIn("nffa", desc)
        self.assertIn("1024×1536", desc)
        self.assertIn("不拼画风前缀", desc)
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

    而它现在更危险——两段那套已经是**全部四个动漫渠道**（默认 `hd_3_clear`）。
    要是把 6.0 顺手挪到它们头上，每张默认图都要先重启一次（60~90 秒），
    比原来的 bug 更糟。

    所以下面测的是**反过来的性质**：机制留着（改 `{}` 即可重现），但对包括
    `hd_3_soft` 在内的任何渠道都不再触发重启。
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

        页面上写 `{"hd_3_soft": 6.0}` 这类「为两段采样预备重启」的配置一律判红——
        那会让默认渠道每张图都白等一分钟。
        """
        self.assertEqual(image_jobs.CLEAN_START_SKILLS, {})

    def test_default_channel_never_restarts_even_on_a_dirty_state(self):
        """hd_3_soft 撞上脏状态（5.6GB）也不重启——它就是两段采样那条，实测够用。"""
        self._run("hd_3_soft", 5.6)
        self.assertEqual(self.restarts, [])
        self.assertEqual(len(self.sent_images), 1)

    def test_default_channel_never_restarts_on_a_critical_state(self):
        """哪怕显存低到 0.5GB 也不为它重启——低水位由 `/free` 那道闸管
        （COMFY_MIN_FREE_VRAM_GB），跟「预备重启」不是一回事。"""
        self._run("hd_3_soft", 0.5)
        self.assertEqual(self.restarts, [])

    def test_unknown_vram_does_not_restart(self):
        """显存问不到就别折腾：那种情况 ComfyUI 多半已经不在了，重启请求
        同样发不出去——照常提交，让 _notice 去说「ComfyUI 没在线」。"""
        self._run("hd_3_soft", None)
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
        self.nai_wide = []          # 每次调用带的 wide 标志（渠道横竖）

        def fake_generate(prompt, wide=False, timeout=None):
            self.nai_calls.append(prompt)
            self.nai_wide.append(wide)
            return b"PNGDATA"

        p = mock.patch.object(nai, "generate", fake_generate)
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
        self.assertEqual(self.nai_wide, [False])    # 竖版渠道
        self.assertEqual(len(self.nai_sent), 1)
        # ComfyUI 那条发图 / 提交路径都没走
        self.assertEqual(len(self.sent_images), 0)
        self.assertEqual(image_jobs.queue_depth(), 0)

    def test_nai_wide_asks_for_landscape(self):
        """`nai_wide` 渠道：还是同一套云端分支，只是让 NAI 出横版。"""
        self._enqueue(("group", "9"), wf="a wide cat", skill="nai_wide")
        image_jobs._drain()
        self.assertEqual(self.nai_calls, ["a wide cat"])
        self.assertEqual(self.nai_wide, [True])
        self.assertEqual(len(self.nai_sent), 1)
        self.assertEqual(len(self.sent_images), 0)
        self.assertEqual(image_jobs.queue_depth(), 0)

    def test_nai_depth_counts_both_orientations(self):
        """状态栏的 nai 队列数把横竖两个渠道一起算（问的是「忙不忙」）。"""
        self._enqueue(("group", "9"), wf="x", skill="nai")
        self._enqueue(("group", "9"), wf="y", skill="nai_wide")
        running, pending = image_jobs.nai_depth()
        self.assertEqual(running + pending, 2)
        image_jobs._drain()
        self.assertEqual(image_jobs.nai_depth(), (0, 0))

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
        self.assertIn("任务已提交", out)
        self.assertTrue(eq.called)
        _, kwargs = eq.call_args
        self.assertEqual(kwargs.get("skill"), "nai")

    def test_nai_wide_enqueues_with_its_own_name(self):
        """横版渠道进同一个云端分支，但 job.skill 要保留自己的渠道名
        （worker 靠它决定文生图的横竖）。"""
        out, eq = self._call("nai_wide", ("group", "9"), nai_ok=True)
        self.assertIn("任务已提交", out)
        self.assertTrue(eq.called)
        _, kwargs = eq.call_args
        self.assertEqual(kwargs.get("skill"), "nai_wide")

    def test_nai_wide_refused_when_not_allowed(self):
        """横竖共用同一套闸：没开通的群传 nai_wide 也不画。"""
        out, eq = self._call("nai_wide", ("group", "9"), nai_ok=False,
                             nai_reason="本群未开通 NAI")
        self.assertIn("本群未开通 NAI", out)
        self.assertFalse(eq.called)

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
        # 横版渠道也必须写进描述，否则模型永远不知道有这么个选择
        self.assertIn("nai_wide", generate_image.tool["description"])
        self.assertIn("nai_wide", generate_image.tool["parameters"]
                      ["properties"]["skill"]["description"])


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
            self._enqueue(skill="hd_3_soft")
            image_jobs._drain()
        line = image_jobs.recent_line("group", "9")
        self.assertIn("已完成：", line)
        self.assertIn("已出图（hd_3_soft）", line)
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
            self._enqueue(skill="hd_3_soft")
            image_jobs._drain()
        line = image_jobs.recent_line("group", "9")
        self.assertIn("失败（hd_3_soft：超时）", line)

    def test_only_the_last_three_and_newest_is_last(self):
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            for _ in range(4):
                self._enqueue(skill="hd_3_soft")
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
            self._enqueue(("group", "9"), skill="hd_3_soft")
            image_jobs._drain()
        self.assertNotEqual(image_jobs.recent_line("group", "9"), "")
        self.assertEqual(image_jobs.recent_line("group", "8"), "")

    def test_web_target_has_no_recall(self):
        # 网页侧同步等结果，模型直接从工具返回值就知道成没成，不需要回执
        with mock.patch.object(image_jobs, "wait_done",
                               return_value=self._entry()):
            self._enqueue((None, None), skill="hd_3_soft")
            image_jobs._drain()
        self.assertEqual(image_jobs.recent_outcomes(None, None), [])
        self.assertEqual(image_jobs.recent_line(None, None), "")

    def test_empty_when_nothing_ran(self):
        self.assertEqual(image_jobs.recent_line("group", "9"), "")


class TaskTimeoutBySkillTest(unittest.TestCase):
    """按渠道分级的出图时限（2026-10-04 加）。

    背景：用户说「我任务都在 80 秒以下」，要全局改 80。但实测日志里
    （`渠道 X，seed Y，耗时 Z 秒` 统计）各渠道中位数差 4 倍——hd_3_clear
    16.6s 而 hd_3_curvy 68.3s，一个全局数必然在两头出错。所以改成查表。

    2026-10-07 用户拍板「全部改成 180 秒，不要区分」→ 分档这套**取消**了，
    SKILL_TIMEOUTS 清空、所有渠道统一走 TASK_TIMEOUT。查表机制本身保留，
    所以这几条改成钉「**不再分档**」+「没配的渠道仍回落全局」。
    """

    def test_every_channel_gets_the_same_limit(self):
        """2026-10-07 用户拍板「全部改成 180 秒，是全部，不要区分」→ 分档表清空，
        所有渠道一律回落 TASK_TIMEOUT（= .env 的 IMAGE_GEN_TIMEOUT，现 180）。

        钉的是**不再分档**这件事：任何渠道都不许再拿一个跟别人不一样的数。
        """
        for skill in ("image_gen_v1", "qwen_image_v1", "nffa", "hd_3_clear",
                      "hd_3_curvy", "hd_3_gloss", "hd_3_soft", "krea2",
                      "cunny", "miao", "silver", "silver-hd", "jank"):
            self.assertEqual(image_jobs.task_timeout(skill),
                             image_jobs.TASK_TIMEOUT, skill)
        self.assertEqual(image_jobs.TASK_TIMEOUT, 180)

    def test_unknown_skill_falls_back_to_global(self):
        """没配的渠道仍走 TASK_TIMEOUT——新渠道不能因为漏配就变慢。"""
        self.assertEqual(image_jobs.task_timeout("将来新增的渠道"),
                         image_jobs.TASK_TIMEOUT)
        self.assertEqual(image_jobs.task_timeout(None), image_jobs.TASK_TIMEOUT)

    def test_accepts_a_job_or_a_skill_name(self):
        """process() 手里有 job，异常路径上只有渠道名——两种都得收。"""
        job = image_jobs.Job("group", "9", {}, skill="hd_3_clear")
        self.assertEqual(image_jobs.task_timeout(job), 180)
        self.assertEqual(image_jobs.task_timeout("hd_3_clear"), 180)

    def test_survives_garbage_input(self):
        """热路径上不能因为一个怪值就抛——抛了整张图的处理就断了。

        只有**不可 hash** 的值会让 `dict.get` 抛 TypeError（实测 object()、123、
        None 都安全返回默认值），所以用例只拿 list/dict 撞——拿别的测不到
        那个 except，测了等于没测。
        """
        # 挡掉真实 HTTP：这条断言不该去连 ComfyUI（慢 40 多秒只为证明一句
        # 纯函数的返回值不抛，不值）。
        with mock.patch.object(image_jobs, "_comfy_up", return_value=True), \
                mock.patch.object(image_jobs, "_restart_comfy"), \
                mock.patch.object(image_jobs, "_free_ram_gb", return_value=9.0), \
                mock.patch.object(image_jobs, "_free_vram_gb", return_value=9.0):
            for junk in (object(), 123, None, ["x"], {"k": 1}, ("t",)):
                self.assertEqual(image_jobs.task_timeout(junk),
                                 image_jobs.TASK_TIMEOUT, repr(junk))

    def test_limit_is_what_wait_done_receives(self):
        """真正传给 wait_done 的必须是查表结果，不是全局值。

        钉这一条是因为超时判定有三条路径（process / _fail_text 的文案 /
        重启原因那句），它们都读同一个函数——但只有 process 那条是
        「真的会等这么久」，其余只是给人看的字。
        """
        seen = []

        def fake_wait_done(prompt_id, timeout=None):
            seen.append(timeout)
            raise TimeoutError("生成超时 (%ds)" % timeout)

        # 2026-10-07 起不再分档：两个渠道拿到的是同一个数（180）。挑一个重的
        # 一个轻的，正是为了钉住「重渠道不再有特权」。
        for skill, want in (("hd_3_clear", 180), ("image_gen_v1", 180)):
            seen.clear()
            job = image_jobs.Job("group", "9", {}, skill=skill)
            job.target = None          # 免得 _notice 真去发消息
            with mock.patch.object(image_jobs, "_maybe_restart_for_clean_start"), \
                    mock.patch.object(image_jobs, "_maybe_release_for_switch"), \
                    mock.patch.object(image_jobs, "_maybe_release_for_low_vram"), \
                    mock.patch.object(image_jobs, "_queue_prompt",
                                      return_value="pid-" + skill), \
                    mock.patch.object(image_jobs, "_comfy_up", return_value=False), \
                    mock.patch.object(image_jobs, "_restart_comfy") as rst, \
                    mock.patch.object(image_jobs, "wait_done", fake_wait_done):
                image_jobs.process(job)
            # 超时后那条分支会探活并可能重启 ComfyUI：探不到活就不重启（真实现
            # 也是这么判的），否则这条用例会白等 COMFY_RESTART_WAIT 那么久。
            self.assertFalse(rst.called, skill)
            self.assertEqual(seen, [want], skill)

    def test_fail_text_uses_the_same_limit(self):
        """给用户看的那句也要跟着查表走，否则说「超过 X 秒」而实际只等了别的数。"""
        for skill in ("hd_3_clear", "image_gen_v1", "将来新增的渠道"):
            self.assertIn("超过 180 秒", image_jobs._fail_text(
                TimeoutError("x"), skill=skill), skill)


class SilentRetryTest(_Base):
    """随机口令被审核拦下 → **静默重抽**（2026-10-05）。

    图是机器人自己推的服务，不该把「未过审」甩给用户：拦下 → resample_fn
    换提示词同 job 重跑（不重新入队、不再扣额度），重抽期间零话术，重抽尽
    才回一句**不提审核**的软话术。用户点的单（无 resample_fn）行为不变。
    """

    def _patch_send(self, results):
        """_send_image 替身：按 results 顺序决定放行/拦截，记录 notify。"""
        seen = []
        def fake_send(target, tid, name, tag="", skill="", seed=None,
                      notify=True, prompt=""):
            seen.append({"notify": notify, "seed": seed})
            return results.pop(0) if results else False
        p = mock.patch.object(image_jobs, "_send_image", fake_send)
        p.start()
        self.addCleanup(p.stop)
        return seen

    def _run(self, resample_fn=None, prompt="p0"):
        entry = {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}
        with mock.patch.object(image_jobs, "wait_done", return_value=entry), \
             mock.patch.object(image_jobs.image_log, "save") as m_save:
            job, reason = image_jobs.enqueue("group", "9", {"1": {}},
                                             "hd_3_clear", prompt=prompt,
                                             resample_fn=resample_fn)
            self.assertIsNone(reason)
            image_jobs._drain()
        return job, m_save

    def test_retry_until_pass_is_silent(self):
        seen = self._patch_send([False, False, True])   # 前两张拦，第三张过
        draws = []
        def resample():
            draws.append(len(draws))
            return "p%d" % (len(draws),)
        job, m_save = self._run(resample_fn=resample)
        self.assertEqual(len(seen), 3)                  # 3 次尝试
        self.assertFalse(seen[0]["notify"])             # 重抽期间全部静默
        self.assertFalse(seen[1]["notify"])
        self.assertFalse(seen[2]["notify"])             # silent 标记全程不变
        self.assertEqual(job.prompt, "p2")              # 账本记的是发出那张
        self.assertEqual(job.retry_left, 0)
        self.assertEqual(self.sent_texts, [])           # 自始至终零话术
        m_save.assert_called_once()                     # 只有发出那张进账本
        self.assertEqual(m_save.call_args.kwargs["prompt"], "p2")

    def test_retry_exhausted_sends_soft_notice(self):
        self._patch_send([])                            # 全拦
        with mock.patch("app.qq_api.send_group") as sg:
            job, _ = self._run(resample_fn=lambda: "pn")
        self.assertEqual(sg.call_args.args,
                         ("9", image_jobs.RANDOM_BLOCKED_NOTICE))
        self.assertNotIn("审核", image_jobs.RANDOM_BLOCKED_NOTICE)
        self.assertEqual(job.retry_left, 0)
        self.assertEqual(job.prompt, "pn")              # 重抽尽：停在最后一条

    def test_resample_returns_empty_falls_back_to_notice(self):
        # 重抽拿不出新提示词 → 不重跑，直接兜底话术
        self._patch_send([False])
        with mock.patch("app.qq_api.send_group") as sg:
            self._run(resample_fn=lambda: "")
        self.assertEqual(sg.call_args.args,
                         ("9", image_jobs.RANDOM_BLOCKED_NOTICE))

    def test_user_job_keeps_old_blocked_behavior(self):
        # 用户点的单（无 resample_fn）：拦下即回 image_audit 的话术，不重抽。
        # 话术本身在 allow_send 里发（此处 mock 掉），worker 层零动作。
        seen = self._patch_send([False])
        with mock.patch("app.qq_api.send_group") as sg:
            self._run()                                 # 不传 resample_fn
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["notify"])
        sg.assert_not_called()


class BuildWorkflowTest(unittest.TestCase):
    """build_t2i_workflow：重抽重建工作流的规则与 _generate_image 共用一份。

    转义规则原来内联在 _generate_image 里，抽出来给 worker 的静默重抽复用——
    这里钉住规则本体：提示词转义、种子落成真数字、占位符零残留。
    """

    def test_escape_seed_and_no_placeholder_left(self):
        wf = generate_image.build_t2i_workflow(
            "hd_3_clear", 'a"b\\c\nd __SEED__ mess', 42)
        s = json.dumps(wf)
        self.assertNotIn("__MULTI_PROMPTS__", s)
        self.assertNotIn("__SEED__", s)                 # 引号版和裸版都清干净
        # 种子替换是**全局**的：提示词里恰好写着 __SEED__ 也会被换掉——
        # 既有行为（与重构前同一份代码），这里钉住它防将来悄悄变向。
        self.assertIn('a\\"b\\\\c\\nd 42 mess', s)
        # 种子是 INT：节点里必须是数字 42，不是字符串 "42"
        seeds = [n["inputs"]["seed"] for n in wf.values()
                 if isinstance(n, dict) and "seed" in n.get("inputs", {})]
        self.assertTrue(seeds and all(v == 42 for v in seeds))
        self.assertTrue(all(isinstance(v, int) for v in seeds))

    def test_missing_skill_returns_none(self):
        self.assertIsNone(generate_image.build_t2i_workflow(
            "no_such_skill_xyz", "x", 1))


class LandscapeTest(_Base):
    """横屏（2026-10-07）：本轮原话说「横屏 / 横版 / 横图」→ 尺寸节点宽高对调。

    判据跟 `generate_image._hd_tier_guard` 同源（读本轮原话
    `qq_api.current_turn_text`），所以「认得出」和「认不出时一个字都不改」
    两件都要钉——后者更要紧，误伤会直接改掉用户没要的尺寸。

    为什么不是每个渠道存一份横版工作流（像 `nai_wide` 那样）：本机 13 个渠道的
    尺寸节点 id 各不相同（8/9/15/23/30/53）、class_type 也有三种，逐个复制要
    手工维护 13 份；对调只需认「哪个节点带 width+height」这一条。
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(qq_api.clear_context)

    def _bind(self, text):
        qq_api.bind_context("group_9", "group", "9", user_text=text)

    # ── 判据 ────────────────────────────────────────────────
    def test_the_three_words_are_recognized(self):
        for t in ("silver 横屏 一个女孩", "anima 横版 女孩", "画个横图"):
            self._bind(t)
            self.assertTrue(image_jobs.turn_is_landscape(), t)

    def test_lookalike_words_are_not_recognized(self):
        """「横向」「横的」不认——它们太容易出现在正常描述里，认了就是误伤。"""
        for t in ("横向构图一个女孩", "横的条纹", "silver 一个女孩", ""):
            self._bind(t)
            self.assertFalse(image_jobs.turn_is_landscape(), t)

    def test_no_turn_text_means_no_change(self):
        """拿不到原话（网页端 / 单测直接调工具）→ 不改尺寸，别替调用方猜。"""
        qq_api.clear_context()
        self.assertFalse(image_jobs.turn_is_landscape())

    # ── 对调 ────────────────────────────────────────────────
    def test_every_local_channel_canvas_is_swapped(self):
        for skill in ("hd_3_clear", "hd_3_soft", "silver", "silver-hd",
                      "image_gen_v1", "jank", "qwen_image_v1", "krea2",
                      "nffa", "cunny", "miao"):
            wf = skills.load_skill(skill)["workflow"]
            nid = image_jobs._find_size_node(wf)
            self.assertIsNotNone(nid, skill)
            ins = wf[nid]["inputs"]
            before = (ins["width"], ins["height"])
            got = image_jobs.landscape_workflow(wf)[nid]["inputs"]
            self.assertEqual((got["width"], got["height"]),
                             (before[1], before[0]), skill)

    def test_the_original_workflow_is_never_mutated(self):
        """返回新对象而不是原地改：静默重抽会重建 `job.workflow` 再递归调
        `process`，原地改会被对调两次（等于没改）。"""
        wf = skills.load_skill("hd_3_clear")["workflow"]
        snapshot = json.loads(json.dumps(wf))
        out = image_jobs.landscape_workflow(wf)
        self.assertIsNot(out, wf)
        self.assertEqual(wf, snapshot)

    def test_hires_pair_follows_the_canvas(self):
        """`BatchPromptImageGenerator` 还带一组 hires_width / hires_height
        （enable_hires 默认 false），一并换掉——不然哪天开了它放大那级还是竖的。"""
        wf = skills.load_skill("jank")["workflow"]
        nid = image_jobs._find_size_node(wf)
        ins = image_jobs.landscape_workflow(wf)[nid]["inputs"]
        self.assertEqual((ins["hires_width"], ins["hires_height"]),
                         (1536, 1024))

    def test_workflow_without_a_size_node_is_untouched(self):
        wf = {"1": {"class_type": "KSampler", "inputs": {"steps": 10}}}
        self.assertIs(image_jobs.landscape_workflow(wf), wf)

    def test_non_numeric_size_is_untouched(self):
        """宽高不是数（模板占位符之类）→ 原样返回，绝不因为这点事把图卡住。"""
        wf = {"9": {"class_type": "EmptyLatentImage",
                    "inputs": {"width": "__W__", "height": 1536}}}
        self.assertIs(image_jobs.landscape_workflow(wf), wf)

    # ── 落到提交上 ──────────────────────────────────────────
    def _enqueue_bound(self, text, skill, workflow=None):
        """在**绑定了本轮原话**的线程里真跑一遍 `enqueue`，返回 job。

        必须走真 `enqueue`——判据是在那儿拍板的（见 `Job.landscape`）。手搓
        `Job` 会绕开被测的那一步：第一版就是这么写的，于是「同线程 process」
        全绿、生产必挂（worker 线程读不到线程本地变量）。
        """
        self._bind(text)
        job, reason = image_jobs.enqueue(
            "group", "9", workflow, skill=skill)
        self.assertIsNone(reason)
        return job

    def _process_and_capture(self, job):
        """真跑一遍 `process`，把交给 ComfyUI 的那份 workflow 抓回来。"""
        seen = []

        def fake_queue(w):
            seen.append(w)
            return "pid"

        job.target = None                   # 免得 _notice 真去发消息
        with mock.patch.object(image_jobs, "_maybe_restart_for_clean_start"), \
                mock.patch.object(image_jobs, "_maybe_release_for_switch"), \
                mock.patch.object(image_jobs, "_maybe_release_for_low_vram"), \
                mock.patch.object(image_jobs, "_queue_prompt", fake_queue), \
                mock.patch.object(image_jobs, "wait_done",
                                  side_effect=TimeoutError("x")), \
                mock.patch.object(image_jobs, "_comfy_up",
                                  return_value=False), \
                mock.patch.object(image_jobs, "_restart_comfy"):
            image_jobs.process(job)
        self.assertEqual(len(seen), 1)
        return seen[0]

    def _queued_canvas(self, text, skill):
        wf = skills.load_skill(skill)["workflow"]
        return self._canvas_of(
            self._process_and_capture(self._enqueue_bound(text, skill, wf)))

    def _canvas_of(self, wf):
        nid = image_jobs._find_size_node(wf)
        ins = wf[nid]["inputs"]
        return (ins["width"], ins["height"])

    def test_process_queues_the_landscape_workflow(self):
        self.assertEqual(self._queued_canvas("silver 横屏 一个女孩", "silver"),
                         (1536, 1024))

    def test_process_keeps_the_canvas_without_the_word(self):
        self.assertEqual(self._queued_canvas("silver 一个女孩", "silver"),
                         (1024, 1536))

    def test_process_swaps_the_anima_canvas_too(self):
        self.assertEqual(
            self._queued_canvas("anima 横屏 一个女孩", "hd_3_clear"),
            (1536, 1024))

    def test_the_decision_is_taken_at_enqueue_time_not_in_the_worker(self):
        """**这就是第一版真正的 bug**（2026-10-07 修）。

        `process()` 跑在常驻的 `image-worker-*` 线程里，而 qq_api 的上下文是
        `threading.local()`、只在 qq_bot 的会话线程（asyncio 事件循环那一条）
        绑过 → 在 worker 里读 `current_turn_text()` 恒为 None，生产上横屏
        **一次都不会生效**。实测：主线程绑 `'silver 横屏 一个女孩'`，子线程
        读出 None。

        所以判据必须在入队时快照。这里把原话清掉（模拟 worker 拿不到上下文）
        再 process——快照还在，尺寸就该照换。
        """
        job = self._enqueue_bound("silver 横屏 一个女孩", "silver",
                                  skills.load_skill("silver")["workflow"])
        qq_api.clear_context()      # 模拟 worker：本轮上下文早没了
        self.assertIsNone(qq_api.current_turn_text())
        self.assertEqual(self._canvas_of(self._process_and_capture(job)),
                         (1536, 1024))

    def test_the_snapshot_survives_a_real_thread_hop(self):
        """复刻生产拓扑：**入队在会话线程，`process` 在另一个线程**。

        第一版在 `process()` 里现读 `current_turn_text()`，这条必挂（worker
        线程读出来是 None）。断言在线程里抛不会传出来，所以自己接住再重抛。
        """
        job = self._enqueue_bound("silver 横屏 一个女孩", "silver",
                                  skills.load_skill("silver")["workflow"])
        box = {}

        def work():
            try:
                box["wf"] = self._process_and_capture(job)
            except BaseException as exc:
                box["err"] = exc

        t = threading.Thread(target=work, name="fake-image-worker")
        t.start()
        t.join()
        if "err" in box:
            raise box["err"]
        self.assertEqual(self._canvas_of(box["wf"]), (1536, 1024))

    def test_enqueue_without_turn_text_stays_portrait(self):
        """网页端 / 单测直接入队：没有原话这个证据源 → 不改尺寸。"""
        qq_api.clear_context()
        job, reason = image_jobs.enqueue("group", "9", {"1": {}}, "silver")
        self.assertIsNone(reason)
        self.assertFalse(job.landscape)

    # ── NAI ────────────────────────────────────────────────
    def _run_nai(self, text, skill="nai"):
        """跑一遍 NAI 分支，返回 `nai.generate` 收到的 wide 值。"""
        job = self._enqueue_bound(text, skill, "a girl")
        job.target = None                   # 出完图就返回，别走发图那半
        with mock.patch.object(nai, "generate",
                               return_value=b"png") as g, \
                mock.patch.object(image_out, "save_bytes",
                                  return_value="/tmp/x.png"):
            image_jobs._process_nai(job)
        return g.call_args.kwargs.get("wide")

    def test_nai_landscape_reuses_the_existing_wide_channel(self):
        """「nai 横屏」= 复用已有的 `nai_wide`（云端只有横竖两档，不新开路径）。"""
        self.assertTrue(self._run_nai("nai 横屏 一个女孩"))

    def test_nai_without_the_word_stays_portrait(self):
        self.assertFalse(self._run_nai("nai 一个女孩"))

    def test_nai_wide_channel_needs_no_word(self):
        """`nai_wide` 是独立渠道名，本来就走横版——别被这次改动弄丢。"""
        self.assertTrue(self._run_nai("随便", skill="nai_wide"))


if __name__ == "__main__":
    unittest.main()
