# -*- coding: utf-8 -*-
"""私聊每日生图额度测试。

用户 2026-09-30 的需求：「私聊除非我给白名单，不然单人每天最多生成 10 个图」。
方案也当场定了三条（都是用户选的）：
1. **接单就扣，失败退还** —— 扣额在真正接单之后（拦得住连点刷队列），没出图就退；
2. **独立免额名单** —— 不复用 private_whitelist（那个管「谁能私聊」，语义不同）；
3. **NAI 一起算** —— 额度是「私聊每天最多几张图」，跟图从本机还是云端出来无关。

对应四处实现：
- `app/image_quota.py` —— 账本（扣 / 退 / 跨天归零 / 坏文件）；
- `app/agents.py` —— 额度闸（只私聊、白名单、开关、默认 10）；
- `app/tools/normal/generate_image.py` —— 接单扣（`_charge_quota` + `_qq_gate`）；
- `app/image_jobs.py` —— 失败退（`_finish`）。

隔离铁律：碰 agents/ 数据必须换整根 AGENTS_DIR；账本 PATH 也要拨到临时目录，
否则会写脏用户真实的 state/image_quota.json。
零网络、零显卡。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.config as config
import app.image_jobs as image_jobs
import app.image_quota as image_quota
import app.main as main
import app.qq_api as qq_api
from app.tools.normal import generate_image


class StateIsolationTest(unittest.TestCase):
    """跑测试不许往真实 state/image_quota.json 里扣假额度（2026-10-04）。

    实测：真实账本里攒了 `"42": 25`——全是测试账号扣的。而额度是**给真人看的
    每日上限**，脏了这个数，别人今天就少画几张。模块默认路径已由
    `config.state_path` 兜住，这条钉住它别再退回去。
    """

    def test_default_path_is_not_the_real_ledger(self):
        self.assertNotEqual(
            os.path.dirname(image_quota.PATH),
            os.path.join(config.BASE_DIR, "state"),
            "测试进程的账本路径落在真实 state/ 下了，跑测试会写脏真实额度")


class _TempQuota(unittest.TestCase):
    """把账本拨到临时目录的公共夹具（不碰真实 state/）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(image_quota, "PATH",
                              os.path.join(self.tmp.name, "quota.json"))
        p.start()
        self.addCleanup(p.stop)
        image_quota.reset()
        self.addCleanup(image_quota.reset)

    def write_ledger(self, date, used):
        """直接写账本文件，模拟「别的进程写的」或「跨天了」。"""
        with open(image_quota.PATH, "w", encoding="utf-8") as f:
            json.dump({"date": date, "used": used}, f)


class QuotaCounterTest(_TempQuota):
    """账本本身：扣、退、跨天归零、坏文件。"""

    def test_charge_counts_up(self):
        self.assertEqual(image_quota.charge("42"), 1)
        self.assertEqual(image_quota.charge("42"), 2)
        self.assertEqual(image_quota.used("42"), 2)

    def test_people_are_counted_separately(self):
        image_quota.charge("42")
        image_quota.charge("43")
        image_quota.charge("43")
        self.assertEqual(image_quota.used("42"), 1)
        self.assertEqual(image_quota.used("43"), 2)

    def test_refund_never_goes_negative(self):
        """退多了不能变负——负账等于额度凭空变多。"""
        image_quota.charge("42")
        self.assertEqual(image_quota.refund("42"), 0)
        self.assertEqual(image_quota.refund("42"), 0)
        self.assertEqual(image_quota.used("42"), 0)

    def test_survives_a_restart(self):
        """机器人重启后额度不能归零——否则重启一下就能刷图。"""
        image_quota.charge("42")
        image_quota.reset()                  # 模拟进程重启，内存镜像清空
        self.assertEqual(image_quota.used("42"), 1)

    def test_disk_is_authoritative_for_other_processes(self):
        """管理页是**另一个进程**、只读不写。它自己的内存镜像从来没被谁扣过，
        所以每次都得先拉磁盘——否则会一整天显示 0。"""
        image_quota.charge("42")
        image_quota.reset()
        self.assertEqual(image_quota.snapshot(), {"42": 1})

    def test_date_rollover_resets_everything(self):
        """跨天归零不靠定时任务：读的时候发现日期不是今天就清空。"""
        self.write_ledger("2000-01-01", {"42": 9})
        image_quota.reset()                  # 清内存，强制下次从磁盘读
        self.assertEqual(image_quota.used("42"), 0)

    def test_corrupt_file_is_treated_as_empty_and_still_usable(self):
        with open(image_quota.PATH, "w", encoding="utf-8") as f:
            f.write("{ 这不是 JSON")
        image_quota.reset()
        self.assertEqual(image_quota.used("42"), 0)
        self.assertEqual(image_quota.charge("42"), 1)   # 还能继续记账

    def test_missing_file_is_fine(self):
        os.remove(image_quota.PATH) if os.path.exists(image_quota.PATH) else None
        self.assertEqual(image_quota.used("42"), 0)
        self.assertEqual(image_quota.charge("42"), 1)

    def test_blank_id_is_a_noop(self):
        self.assertEqual(image_quota.charge(""), 0)
        self.assertEqual(image_quota.refund(""), 0)
        self.assertEqual(image_quota.used(""), 0)


class QuotaGateTest(_TempQuota):
    """agents.image_quota_allowed：只私聊、白名单、开关、默认 10。"""

    def setUp(self):
        super().setUp()
        self.settings = {}
        p = mock.patch.object(agents, "load_settings",
                              side_effect=lambda aid: dict(self.settings))
        p.start()
        self.addCleanup(p.stop)

    def allowed(self, target="private", target_id="42"):
        return agents.image_quota_allowed("qq", target, target_id)

    # ── 只管私聊 ──

    def test_group_is_never_limited(self):
        self.settings = {"private_image_daily_limit": 1}
        for _ in range(50):
            image_quota.charge("42")
        self.assertEqual(self.allowed("group", "42"), (True, ""))

    def test_web_target_is_never_limited(self):
        self.settings = {"private_image_daily_limit": 1}
        image_quota.charge("42")
        self.assertEqual(self.allowed(None, "42"), (True, ""))

    # ── 基本放行 / 拦截 ──

    def test_private_under_limit_is_allowed(self):
        self.settings = {"private_image_daily_limit": 3}
        image_quota.charge("42")
        self.assertEqual(self.allowed(), (True, ""))

    def test_private_at_limit_is_refused(self):
        self.settings = {"private_image_daily_limit": 3}
        for _ in range(3):
            image_quota.charge("42")
        ok, why = self.allowed()
        self.assertFalse(ok)
        self.assertIn("3/3", why)

    # ── 免额名单 ──

    def test_whitelist_is_exempt(self):
        self.settings = {"private_image_daily_limit": 1,
                         "private_image_quota_whitelist": ["42"]}
        for _ in range(9):
            image_quota.charge("42")
        self.assertEqual(self.allowed(), (True, ""))

    def test_whitelist_only_exempts_the_listed_number(self):
        self.settings = {"private_image_daily_limit": 1,
                         "private_image_quota_whitelist": ["42"]}
        image_quota.charge("43")
        ok, _ = self.allowed(target_id="43")
        self.assertFalse(ok)

    # ── 开关与 0 ──

    def test_switch_off_means_unlimited(self):
        self.settings = {"private_image_daily_limit": 1,
                         "private_image_quota_on": False}
        for _ in range(9):
            image_quota.charge("42")
        self.assertEqual(self.allowed(), (True, ""))

    def test_zero_limit_means_unlimited(self):
        self.settings = {"private_image_daily_limit": 0}
        for _ in range(9):
            image_quota.charge("42")
        self.assertEqual(self.allowed(), (True, ""))

    # ── 默认值与坏值 ──

    def test_default_limit_is_ten(self):
        self.assertEqual(agents.PRIVATE_IMAGE_DAILY_LIMIT_DEFAULT, 10)
        self.assertEqual(agents.private_image_daily_limit("qq"), 10)

    def test_broken_limit_falls_back_to_default_not_unlimited(self):
        """手滑写成字符串时回落默认 10，**不是**回落「不限」——否则限流会被
        一个 typo 悄悄关掉，而用户还以为开着。"""
        self.settings = {"private_image_daily_limit": "十张"}
        self.assertEqual(agents.private_image_daily_limit("qq"), 10)

    def test_raw_limit_keeps_the_number_when_switch_is_off(self):
        """管理页输入框要回显原始数字，开关关掉也不能变成 0。"""
        self.settings = {"private_image_daily_limit": 7,
                         "private_image_quota_on": False}
        self.assertEqual(agents.private_image_daily_limit("qq"), 0)
        self.assertEqual(agents.private_image_daily_limit_raw("qq"), 7)

    def test_quota_info_for_admin_page(self):
        self.settings = {"private_image_daily_limit": 5}
        image_quota.charge("42")
        self.assertEqual(agents.private_image_quota_info("qq", "42"),
                         {"limit": 5, "used": 1, "whitelisted": False})


class QuotaLineTest(_TempQuota):
    """agents.image_quota_line：给模型的「当前额度状态」锚点。

    2026-10-01 用户报「加了白名单，AI 还说我限额了」。查下来额度闸没问题
    （白名单确实放行），坏在模型侧：那句「额度用完了」作为 tool_result 留在
    会话历史里，模型之后一直照着它回话，连工具都不再调一次。这行的意义就是
    让**当下**每轮都能压过历史，所以每种状态都钉一遍——尤其白名单那条，
    必须明说旧拒绝作废，否则模型会跟历史里那句打架。
    """

    def setUp(self):
        super().setUp()
        self.settings = {}
        p = mock.patch.object(agents, "load_settings",
                              side_effect=lambda aid: dict(self.settings))
        p.start()
        self.addCleanup(p.stop)

    def line(self, target="private", target_id="42"):
        return agents.image_quota_line("qq", target, target_id)

    def test_group_and_web_get_nothing(self):
        """额度只管私聊，群聊/网页端不该多出这一行白占尾巴。"""
        self.settings = {"private_image_daily_limit": 1}
        self.assertEqual(self.line("group", "42"), "")
        self.assertEqual(self.line(None, "42"), "")

    def test_quota_off_still_speaks(self):
        """限流关掉**不能**返回空串（2026-10-01 二次修）。

        原版这里断言 `== ""`，理由是「没什么可说就别占尾巴」。但空串对模型
        来说不是「省字」而是「沉默」：管理员临时关掉限流救人时，历史里那句
        「额度用完」成了唯一还看得见的信号，于是又犯「加了白名单还说我限额」
        那个 bug——同一类，只是这次是关开关触发的。
        """
        self.settings = {"private_image_daily_limit": 0}
        s = self.line()
        self.assertIn("限流关掉", s)
        self.assertIn("以它为准", s)

    def test_switch_off_gets_the_same_line(self):
        """`private_image_quota_on: False` 和「上限 0」是同一件事，都要说话。"""
        self.settings = {"private_image_daily_limit": 5,
                         "private_image_quota_on": False}
        self.assertIn("限流关掉", self.line())

    def test_under_limit_says_how_many_left(self):
        self.settings = {"private_image_daily_limit": 3}
        image_quota.charge("42")
        s = self.line()
        self.assertIn("1/3", s)
        self.assertIn("还能画 2 张", s)

    def test_under_limit_says_the_charge_is_at_submit_time(self):
        """「已用 2/3」里有扣了但图还没回来的。不点明，对方一说「没收到图」
        模型就以为账错了，跟着重画。"""
        self.settings = {"private_image_daily_limit": 3}
        self.assertIn("接单就扣", self.line())

    def test_inflight_jobs_are_broken_out(self):
        """在途几张要单列——它是「已用」和「已收到」之间那段差额的唯一出处。"""
        self.settings = {"private_image_daily_limit": 5}
        image_quota.charge("42")
        image_quota.charge("42")
        with mock.patch.object(image_jobs, "inflight_count",
                               return_value=2):
            s = self.line()
        self.assertIn("其中 2 张还在排队", s)
        self.assertIn("没收到图", s)

    def test_no_inflight_line_when_nothing_is_running(self):
        """没有在途就别硬加一句「其中 0 张」——那是噪声。"""
        self.settings = {"private_image_daily_limit": 5}
        image_quota.charge("42")
        self.assertNotIn("还在排队", self.line())

    def test_at_limit_says_exhausted(self):
        self.settings = {"private_image_daily_limit": 3}
        for _ in range(3):
            image_quota.charge("42")
        self.assertIn("已经用满", self.line())

    def test_exhausted_names_the_reset_moment(self):
        """「明天恢复」要说清是**几点**。账本按本地日期翻页归零（00:00），
        不说模型就会许「两小时后再来」这种做不到的承诺。"""
        self.settings = {"private_image_daily_limit": 1}
        image_quota.charge("42")
        self.assertIn("00:00", self.line())

    def test_every_line_says_it_outranks_history(self):
        """每种状态都得带上「以它为准」——模型手里同时有这行（每轮新）和
        历史里的旧拒绝（永不消失），不点名谁大，它常常挑旧的说。"""
        cases = (
            ("没用满", {"private_image_daily_limit": 3}, 1),
            ("用满", {"private_image_daily_limit": 1}, 1),
            ("免额", {"private_image_daily_limit": 1,
                      "private_image_quota_whitelist": ["42"]}, 1),
        )
        for name, settings, charges in cases:
            with self.subTest(name):
                self.settings = settings
                image_quota.reset()
                for _ in range(charges):
                    image_quota.charge("42")
                self.assertIn("以它为准", self.line())

    def test_whitelisted_says_unlimited_and_voids_old_refusal(self):
        """白名单这条只写「不限量」不够——必须同时**明说历史里的旧拒绝不作数**，
        不然模型手里是两个互相矛盾的信号，还是会挑那个旧的说。"""
        self.settings = {"private_image_daily_limit": 1,
                         "private_image_quota_whitelist": ["42"]}
        for _ in range(9):
            image_quota.charge("42")
        s = self.line()
        self.assertIn("不限量", s)
        self.assertIn("不作数", s)


class ChargeWiringTest(_TempQuota):
    """generate_image._charge_quota：只私聊扣，并留下可退款的凭据。"""

    def test_private_job_is_charged_and_marked(self):
        job = image_jobs.Job("private", "42", {}, skill="anima_clear")
        generate_image._charge_quota(job, "private", "42")
        self.assertEqual(image_quota.used("42"), 1)
        self.assertTrue(job.quota_charged)

    def test_group_job_is_not_charged(self):
        job = image_jobs.Job("group", "1", {}, skill="anima_clear")
        generate_image._charge_quota(job, "group", "1")
        self.assertEqual(image_quota.used("1"), 0)
        self.assertFalse(job.quota_charged)

    def test_web_job_is_not_charged(self):
        job = image_jobs.Job(None, None, {}, skill="anima_clear")
        generate_image._charge_quota(job, None, None)
        self.assertFalse(job.quota_charged)


class QuotaReceiptTailTest(_TempQuota):
    """提交回执末尾那句实时余额。

    额度行（`agents.image_quota_line`）一轮只算一次、算的是开跑**之前**的数，
    而额度闸每次调工具都现读账本。同一轮连画两张时两者就会打架（行说「还能画
    3 张」、工具拒了）。把刚扣完的数拼进回执，模型手里就有跟闸同源的最新值。
    """

    def setUp(self):
        super().setUp()
        self.settings = {"private_image_daily_limit": 3}
        p = mock.patch.object(agents, "load_settings",
                              side_effect=lambda aid: dict(self.settings))
        p.start()
        self.addCleanup(p.stop)

    def test_charge_returns_the_fresh_balance(self):
        job = image_jobs.Job("private", "42", {}, skill="anima_clear")
        tail = generate_image._charge_quota(job, "private", "42")
        self.assertIn("1/3", tail)
        self.assertIn("剩 2 张", tail)

    def test_second_submit_in_the_same_turn_shows_the_new_number(self):
        """这一条就是它存在的理由：同轮两张，回执里的数必须往前走。"""
        for used, left in ((1, 2), (2, 1), (3, 0)):
            job = image_jobs.Job("private", "42", {}, skill="anima_clear")
            tail = generate_image._charge_quota(job, "private", "42")
            with self.subTest(used=used):
                self.assertIn("%d/3" % used, tail)
                self.assertIn("剩 %d 张" % left, tail)

    def test_group_and_web_get_an_empty_tail(self):
        for target, tid in (("group", "9"), (None, None)):
            job = image_jobs.Job(target, tid, {}, skill="anima_clear")
            self.assertEqual(generate_image._charge_quota(job, target, tid), "")

    def test_exempt_users_get_no_number(self):
        """免额名单 / 限流关掉时别报「剩 999 张」，那种情况下没数可报。"""
        for settings in ({"private_image_daily_limit": 3,
                          "private_image_quota_whitelist": ["42"]},
                         {"private_image_daily_limit": 0}):
            self.settings = settings
            image_quota.reset()
            job = image_jobs.Job("private", "42", {}, skill="anima_clear")
            self.assertEqual(generate_image._charge_quota(job, "private", "42"),
                             "")

    def test_nai_receipt_carries_the_balance(self):
        """NAI 也拼（用户选的「一起算」）——它同样占私聊额度。

        回执本体自 2026-10-04 起由工具**直接发**（`_qq_receipt`），所以这里把
        发送挡掉、并顺手断言它真的直发了（不经模型那张嘴）。
        """
        job = image_jobs.Job("private", "42", {}, skill="nai")
        sent = []
        with mock.patch.object(image_jobs, "enqueue", return_value=(job, None)), \
                mock.patch.object(image_jobs, "_send_text",
                                  lambda t, tid, x: sent.append((t, tid, x))):
            out = generate_image._enqueue_nai("a girl", "private", "42")
        self.assertIn("任务已提交", out)
        self.assertIn("剩 2 张", out)
        self.assertEqual([(t, tid) for t, tid, _ in sent], [("private", "42")])

    def test_comfyui_receipt_carries_the_balance(self):
        """本机渠道同上（`target is not None` 那条分支）。

        mock 的套路照 `DisabledChannelTest`：探活 / worker / 发图全挡掉，
        让 `enqueue` 真的把任务排进内存队列，这样回执走的是本机那条。
        """
        from app.tools.normal import generate_image as gi
        for target, repl in (("comfy_alive", mock.Mock(return_value=True)),
                             ("_ensure_worker", lambda: None),
                             ("_send_image", mock.Mock()),
                             ("_send_text", mock.Mock()),
                             ("_free_vram_gb", lambda: None)):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)
        for target, repl in (("load_skill",
                              mock.Mock(return_value={"workflow": {"1": {}}})),
                             ("_qq_gate", mock.Mock(return_value=None)),
                             ("is_cancelled", mock.Mock(return_value=False))):
            p = mock.patch.object(gi, target, repl)
            p.start()
            self.addCleanup(p.stop)
        image_jobs._reset()
        self.addCleanup(image_jobs._reset)
        with mock.patch.object(qq_api, "current_context",
                               return_value=("private", "42")):
            out = gi.tool["function"](prompt="a cat", skill="anima_clear")
        self.assertIn("任务已提交", out)
        self.assertIn("剩 2 张", out)
        self.assertEqual(image_quota.used("42"), 1)


class QqGateMessageTest(_TempQuota):
    """_qq_gate 额度用完时返回一句模型能直接转述的话。"""

    def setUp(self):
        super().setUp()
        self.settings = {"private_image_daily_limit": 1}
        p = mock.patch.object(agents, "load_settings",
                              side_effect=lambda aid: dict(self.settings))
        p.start()
        self.addCleanup(p.stop)

    def test_over_quota_message_tells_the_model_what_to_say(self):
        image_quota.charge("42")
        with mock.patch.object(qq_api, "current_context",
                               return_value=("private", "42")):
            out = generate_image._qq_gate()
        self.assertIsNotNone(out)
        self.assertIn("额度", out)
        # 出路必须是「明天再来」，不是「机器坏了」——后者会让对方反复重试
        self.assertIn("明天", out)
        self.assertIn("别再重试", out)
        # 「这一轮」这个限定词别删：不限定的话模型会读成**永久**禁令，之后
        # 管理员把人加进免额名单了它也不再调一次工具确认（2026-10-01 用户报）。
        self.assertIn("这一轮", out)

    def test_under_quota_passes_the_gate(self):
        with mock.patch.object(qq_api, "current_context",
                               return_value=("private", "42")):
            self.assertIsNone(generate_image._qq_gate())

    def test_group_is_never_gated_by_quota(self):
        for _ in range(5):
            image_quota.charge("1")
        with mock.patch.object(qq_api, "current_context",
                               return_value=("group", "1")):
            self.assertIsNone(generate_image._qq_gate())


class RefundOnFailureTest(_TempQuota):
    """image_jobs._finish：没出图就退还，出图了就留着。"""

    def _job(self, charged=True):
        job = image_jobs.Job("private", "42", {}, skill="anima_clear")
        job.quota_charged = charged
        return job

    def test_failed_job_refunds(self):
        image_quota.charge("42")
        job = self._job()
        job.error = RuntimeError("跑完了但没找到图片")
        image_jobs._finish(job)
        self.assertEqual(image_quota.used("42"), 0)

    def test_successful_job_keeps_the_charge(self):
        image_quota.charge("42")
        job = self._job()
        job.skill_done = True
        image_jobs._finish(job)
        self.assertEqual(image_quota.used("42"), 1)

    def test_undelivered_image_also_refunds(self):
        """图出来了但没发回会话（对方不是好友）——对用户等于没画，要退。

        这条和 2026-09-30 那个 bug 是同一件事：协议端拒收时 `job.error` 会被
        置上（见 image_jobs.process 的 stage="send"），所以判据天然覆盖到它。
        """
        image_quota.charge("42")
        job = self._job()
        job.skill_done = True
        job.error = RuntimeError("send_private_msg 失败")
        image_jobs._finish(job)
        self.assertEqual(image_quota.used("42"), 0)

    def test_unmarked_job_is_never_refunded(self):
        """网页端 / 群聊的任务没扣过，_finish 不能凭空把账退掉。"""
        image_quota.charge("42")
        job = self._job(charged=False)
        job.error = RuntimeError("boom")
        image_jobs._finish(job)
        self.assertEqual(image_quota.used("42"), 1)

    def test_successful_job_leaves_others_untouched(self):
        image_quota.charge("42")
        image_quota.charge("43")
        job = self._job()
        job.skill_done = True
        image_jobs._finish(job)
        self.assertEqual(image_quota.snapshot(), {"42": 1, "43": 1})


class QuotaAdminApiTest(unittest.TestCase):
    """管理页三个接口：改开关 / 改数字 / 免额名单增删。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        p = mock.patch.object(main, "ADMIN_ALLOW_REMOTE", True)
        p.start()
        self.addCleanup(p.stop)
        self.client = main.app.test_client()

    def url(self, path):
        return "/api/agent/qq/" + path

    def test_put_limit_and_read_it_back(self):
        r = self.client.put(self.url("private_image_quota"), json={"limit": 5})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["private_image_daily_limit"], 5)
        self.assertEqual(agents.private_image_daily_limit("qq"), 5)

    def test_rejects_bad_limit_values(self):
        # True 是 int 的子类，必须单独挡掉，否则会变成「1 张」
        for bad in (-1, "5", 1.5, True, None):
            r = self.client.put(self.url("private_image_quota"), json={"limit": bad})
            self.assertEqual(r.status_code, 400, "limit=%r 应当被拒" % (bad,))

    def test_switch_off_keeps_the_number_for_the_input_box(self):
        """关掉限流不能把数字弄丢，否则「临时放开」就变成「配置没了」。"""
        self.client.put(self.url("private_image_quota"), json={"limit": 7})
        r = self.client.put(self.url("private_image_quota"), json={"enabled": False})
        self.assertEqual(r.get_json()["private_image_daily_limit"], 7)
        self.assertFalse(r.get_json()["private_image_quota_on"])
        self.assertEqual(agents.private_image_daily_limit("qq"), 0)   # 生效值 = 不限

    def test_empty_body_is_rejected(self):
        r = self.client.put(self.url("private_image_quota"), json={})
        self.assertEqual(r.status_code, 400)

    def test_whitelist_add_and_remove(self):
        r = self.client.put(self.url("private_image_quota_whitelist/42"),
                            json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["whitelisted"])
        self.assertEqual(agents.private_image_quota_whitelist("qq"), {"42"})
        r = self.client.put(self.url("private_image_quota_whitelist/42"),
                            json={"enabled": False})
        self.assertFalse(r.get_json()["whitelisted"])
        self.assertEqual(agents.private_image_quota_whitelist("qq"), set())

    def test_whitelist_needs_bool(self):
        r = self.client.put(self.url("private_image_quota_whitelist/42"),
                            json={"enabled": "yes"})
        self.assertEqual(r.status_code, 400)

    def test_quota_is_independent_from_the_private_dm_whitelist(self):
        """两条需求刻意分开：加进「能私聊」名单**不等于**生图免额。"""
        self.client.put(self.url("private_whitelist"),
                        json={"op": "add", "user_id": "42"})
        self.assertEqual(agents.private_image_quota_whitelist("qq"), set())


class PrivateSendFailureTest(unittest.TestCase):
    """私聊发不出去（对方不是好友）要能被认出来——2026-09-30 那个 bug。"""

    def test_recognises_the_friend_required_error(self):
        exc = RuntimeError(
            "OneBot 调用失败 send_private_msg: {'status': 'failed', "
            "'retcode': 100, 'wording': 'send private message rejected: "
            "result=16 err=发送失败，请先添加对方为好友'}")
        self.assertTrue(qq_api.friend_required_error(exc))

    def test_recognises_the_oidb_verify_identify_wording(self):
        """**第二种**措辞（2026-10-01 03:44）：图和文字拿到的居然不一样。

        同一秒、同一个非好友：图回 `verify identify fail`，文字回
        `请先添加对方为好友`。只认后者的代价是图这条直接报失败——而图其实
        已经画好了（Anima_00276_.png，3.89MB）。
        """
        exc = RuntimeError(
            "OneBot 调用失败 send_private_msg: {'status': 'failed', "
            "'retcode': 100, 'data': None, 'wording': 'OIDB error 170019003 "
            "on 0x11c5_100: verify identify fail'}")
        self.assertTrue(qq_api.friend_required_error(exc))

    def test_other_failures_are_not_mistaken_for_it(self):
        for msg in ("OneBot 调用失败 send_private_msg: retcode=100 风控",
                    "HTTPConnectionPool 连不上",
                    ""):
            self.assertFalse(qq_api.friend_required_error(RuntimeError(msg)))

    def test_a_plain_oidb_error_is_not_enough(self):
        """只认「verify identify fail」这个身份校验失败，别的 OIDB 错不算。"""
        exc = RuntimeError("OIDB error 170019003 on 0x11c5_100: unknown")
        self.assertFalse(qq_api.friend_required_error(exc))

    def test_non_private_targets_are_left_alone(self):
        import app.qq_bot as qq_bot
        exc = RuntimeError("请先添加对方为好友")
        self.assertFalse(qq_bot._note_private_send_failure("group", "1", exc))
