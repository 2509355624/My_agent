"""看门狗逻辑测试：只驱动 _cycle / _probe，把网络探测 / 重启 / 推送全 mock 掉。

覆盖：三态探测（online / offline / dead）、未到阈值不重启、到阈值触发重启并恢复、
登录态失效满宽限期才重启、重启后要扫码只推一条合并通知、一直没人扫码会退避、
重启连续失败放弃并退避、退避期内不动作。不涉及真实 NapCat / subprocess。
"""

from unittest import TestCase, mock

from app import watchdog as wd


class ProbeStateTest(TestCase):
    """_probe 的三态：用 6099 WebUI 口区分「进程死了」和「活着但没登录」。"""

    def setUp(self):
        p = mock.patch.object(wd, "check_alive")
        self.alive = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(wd, "_port_open")
        self.webui = p.start()
        self.addCleanup(p.stop)

    def test_online(self):
        self.alive.return_value = (3842266925, "小小怪")
        self.assertEqual(wd._probe()[0], "online")

    def test_empty_uid_is_offline(self):
        """拿得到响应但没有登录态 → 离线，不是死。"""
        self.alive.return_value = (0, "")
        self.assertEqual(wd._probe()[0], "offline")

    def test_connect_error_with_webui_up_is_offline(self):
        """:3000 只在登录成功后才监听 —— 连不上但 6099 在听 = 没登录，进程还活着。"""
        self.alive.side_effect = OSError("refused")
        self.webui.return_value = True
        self.assertEqual(wd._probe()[0], "offline")

    def test_connect_error_without_webui_is_dead(self):
        self.alive.side_effect = OSError("refused")
        self.webui.return_value = False
        self.assertEqual(wd._probe()[0], "dead")


class WatchdogCycleTest(TestCase):

    def setUp(self):
        self.patchers = [
            mock.patch.object(wd, "_acquire_lock", lambda: None),
            mock.patch.object(wd, "_probe"),
            mock.patch.object(wd, "_trigger_restart", return_value=True),
            mock.patch.object(wd, "_await_outcome", return_value="online"),
            mock.patch.object(wd, "push_text", return_value=(True, "ok")),
            # 别在测试里真的去建 / 删 state/restart.flag
            mock.patch.object(wd.notify, "mark_restarting"),
            mock.patch.object(wd.notify, "clear_restarting"),
            mock.patch.object(wd.notify, "push_offline", return_value=(True, "ok")),
            # 静默判据另开一组（SilenceProbeTest）：这里关掉，免得去读真快照文件
            mock.patch.object(wd, "_silent_seconds", return_value=None),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    @staticmethod
    def new_state():
        return {"fails": 0, "restart_fails": 0, "offline_since": None,
                "backoff_until": 0.0, "silence_until": 0.0, "silence_gap": 0.0}

    def test_below_threshold_no_restart(self):
        wd._probe.side_effect = [("dead", "x"), ("dead", "x"), ("online", "ok")]
        s = self.new_state()
        wd._cycle(s, now=0.0)   # fail 1
        wd._cycle(s, now=1.0)   # fail 2
        wd._cycle(s, now=2.0)   # ok -> reset
        self.assertEqual(s["fails"], 0)
        wd._trigger_restart.assert_not_called()

    def test_threshold_triggers_restart_then_recovers(self):
        wd._probe.side_effect = [("dead", "x"), ("dead", "x"), ("dead", "x"),
                                 ("online", "ok")]
        s = self.new_state()
        for t in range(3):              # 连续 3 次失败
            wd._cycle(s, now=float(t))
        wd._trigger_restart.assert_called_once()
        wd._await_outcome.assert_called_once()
        wd._cycle(s, now=10.0)          # 恢复在线
        # 自动恢复**不通知**（09-29 用户要求：能自己登回来就别打扰）
        self.assertEqual(wd.push_text.call_count, 0)

    def test_no_premature_restart_message(self):
        """重启一发起不先发「正在自动重启」——只发结论那一条。"""
        wd._probe.return_value = ("dead", "x")
        s = self.new_state()
        for t in range(3):
            wd._cycle(s, now=float(t))
        titles = [c.args[0] for c in wd.push_text.call_args_list]
        self.assertNotIn("QQ 机器人正在自动重启", titles)

    def test_offline_within_grace_does_not_restart(self):
        """刚发现没登录时先别动 —— 人可能正在扫，NapCat 也可能正在快速登录。"""
        wd._probe.return_value = ("offline", "no login")
        s = self.new_state()
        wd._cycle(s, now=0.0)
        wd._cycle(s, now=wd.OFFLINE_GRACE - 1)
        wd._trigger_restart.assert_not_called()
        self.assertEqual(s["offline_since"], 0.0)

    def test_offline_past_grace_triggers_restart(self):
        """码过期了还没人扫 → 重启换一张新码（用户 09-29 明确要求）。"""
        wd._probe.return_value = ("offline", "no login")
        s = self.new_state()
        wd._cycle(s, now=0.0)
        wd._cycle(s, now=wd.OFFLINE_GRACE + 1)
        wd._trigger_restart.assert_called_once()

    def test_offline_grace_restarts_after_online(self):
        """中途回过在线 → 离线计时清零，不能拿旧起点凑满宽限期。"""
        wd._probe.side_effect = [("offline", ""), ("online", "ok"),
                                 ("offline", ""), ("offline", "")]
        s = self.new_state()
        wd._cycle(s, now=0.0)
        wd._cycle(s, now=10.0)
        self.assertIsNone(s["offline_since"])
        wd._cycle(s, now=1000.0)                       # 重新起算
        wd._cycle(s, now=1000.0 + wd.OFFLINE_GRACE - 1)
        wd._trigger_restart.assert_not_called()

    def test_dead_resets_offline_clock(self):
        wd._probe.side_effect = [("offline", ""), ("dead", "x")]
        s = self.new_state()
        wd._cycle(s, now=0.0)
        wd._cycle(s, now=1.0)
        self.assertIsNone(s["offline_since"])

    def test_qr_outcome_sends_one_merged_notification(self):
        """重启后要扫码：只推一条带二维码的通知，不再另发「已恢复」。"""
        wd._await_outcome.return_value = "qr"
        wd._probe.return_value = ("dead", "x")
        s = self.new_state()
        for t in range(3):
            wd._cycle(s, now=float(t))
        wd.notify.push_offline.assert_called_once()
        self.assertIn("扫码", wd.notify.push_offline.call_args.kwargs["title"])
        self.assertTrue(wd.notify.push_offline.call_args.kwargs["intro"])
        self.assertEqual(wd.push_text.call_count, 0)
        self.assertEqual(s["restart_fails"], 1)

    def test_restart_flag_handed_over_and_released(self):
        """重启窗口期把二维码通知权接管过来，无论结果如何都要还回去。"""
        wd._probe.return_value = ("dead", "x")
        s = self.new_state()
        for t in range(3):
            wd._cycle(s, now=float(t))
        wd.notify.mark_restarting.assert_called_once()
        wd.notify.clear_restarting.assert_called_once()

    def test_restart_flag_released_when_trigger_fails(self):
        wd._trigger_restart.return_value = False
        wd._probe.return_value = ("dead", "x")
        s = self.new_state()
        for t in range(3):
            wd._cycle(s, now=float(t))
        wd.notify.clear_restarting.assert_called_once()
        wd._await_outcome.assert_not_called()

    def test_give_up_after_max_restart_failures(self):
        wd._probe.return_value = ("dead", "x")   # 永远失败
        wd._await_outcome.return_value = "unknown"   # 重启也救不活
        s = self.new_state()
        for t in range(9):               # 3 轮失败 = 3 次触发
            wd._cycle(s, now=float(t))
        self.assertEqual(wd._trigger_restart.call_count, wd.MAX_RESTART_FAILS)
        self.assertGreater(s["backoff_until"], 0.0)
        wd.push_text.assert_any_call(
            "QQ 机器人需人工处理",
            "看门狗多次自动重启失败，NapCat 仍无法恢复，请检查。")

    def test_repeated_qr_outcomes_back_off(self):
        """一直没人扫码：重启 N 次后暂停自动重启，别再整夜反复杀进程。"""
        wd._probe.return_value = ("offline", "no login")
        wd._await_outcome.return_value = "qr"
        s = self.new_state()
        t = 0.0
        for _ in range(wd.MAX_RESTART_FAILS):
            wd._cycle(s, now=t)                      # 起算离线
            wd._cycle(s, now=t + wd.OFFLINE_GRACE + 1)
            t += 10 * wd.OFFLINE_GRACE               # 跳到下一轮，别撞上退避
        self.assertEqual(wd._trigger_restart.call_count, wd.MAX_RESTART_FAILS)
        self.assertGreater(s["backoff_until"], 0.0)
        wd.push_text.assert_any_call(
            "QQ 机器人仍在等待扫码",
            "已连续自动重启 %d 次仍停在扫码界面，暂停自动重启 %.0f 分钟；"
            "二维码换新后仍会推给你。"
            % (wd.MAX_RESTART_FAILS, wd.BACKOFF / 60))

    def test_online_clears_backoff(self):
        """真恢复了就把退避清掉，别让上一轮的退避耽误下一次掉线。"""
        s = self.new_state()
        s["backoff_until"] = 500.0
        s["restart_fails"] = 1
        wd._probe.return_value = ("online", "ok")
        wd._cycle(s, now=600.0)
        self.assertEqual(s["backoff_until"], 0.0)

    def test_backoff_suppresses_action(self):
        wd._probe.return_value = ("dead", "x")
        s = self.new_state()
        s["backoff_until"] = 1000.0      # 退避中
        wd._cycle(s, now=500.0)
        wd._trigger_restart.assert_not_called()

    def test_recovers_without_trigger_after_backoff(self):
        # 退避期过后、还没到失败阈值时自己恢复了
        wd._probe.side_effect = [("dead", "x"), ("online", "ok")]
        s = self.new_state()
        s["backoff_until"] = 0.0
        s["restart_fails"] = 1           # 上一轮重启失败过
        wd._cycle(s, now=0.0)            # fail 1（未到阈值，不重启）
        wd._cycle(s, now=1.0)            # 恢复
        wd._trigger_restart.assert_not_called()
        self.assertEqual(s["restart_fails"], 0)
        wd.push_text.assert_not_called()      # 恢复不通知


class SilenceProbeTest(TestCase):
    """静默判据（假在线）：三态探针探不出「接口全好但收不到消息」。

    重启当探针 —— 自动登录 = 群里本来就安静（静默放过、不通知），
    要扫码 = 会话真被腾讯作废（推二维码）。
    """

    def setUp(self):
        self.patchers = [
            mock.patch.object(wd, "_acquire_lock", lambda: None),
            mock.patch.object(wd, "_probe", return_value=("online", "ok")),
            mock.patch.object(wd, "_trigger_restart", return_value=True),
            mock.patch.object(wd, "_await_outcome", return_value="online"),
            mock.patch.object(wd, "push_text", return_value=(True, "ok")),
            mock.patch.object(wd.notify, "mark_restarting"),
            mock.patch.object(wd.notify, "clear_restarting"),
            mock.patch.object(wd.notify, "push_offline", return_value=(True, "ok")),
            mock.patch.object(wd, "_silent_seconds"),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    @staticmethod
    def new_state():
        return {"fails": 0, "restart_fails": 0, "offline_since": None,
                "backoff_until": 0.0, "silence_until": 0.0, "silence_gap": 0.0}

    def test_below_threshold_no_restart(self):
        wd._silent_seconds.return_value = wd.SILENCE_SECONDS - 1
        s = self.new_state()
        wd._cycle(s, now=1000.0)
        wd._trigger_restart.assert_not_called()

    def test_past_threshold_restarts_silently(self):
        """静默超阈值 → 重启当探针；自动登录回来不通知。"""
        wd._silent_seconds.return_value = wd.SILENCE_SECONDS + 1
        s = self.new_state()
        wd._cycle(s, now=1000.0)
        wd._trigger_restart.assert_called_once()
        wd.push_text.assert_not_called()

    def test_qr_outcome_notifies(self):
        """要扫码 = 会话真被作废（假在线坐实）→ 必须通知。"""
        wd._silent_seconds.return_value = wd.SILENCE_SECONDS + 1
        wd._await_outcome.return_value = "qr"
        s = self.new_state()
        wd._cycle(s, now=1000.0)
        wd.notify.push_offline.assert_called_once()
        self.assertEqual(s["restart_fails"], 1)

    def test_cooldown_suppresses_repeat_restart(self):
        wd._silent_seconds.return_value = wd.SILENCE_SECONDS + 1
        s = self.new_state()
        wd._cycle(s, now=1000.0)
        wd._trigger_restart.assert_called_once()
        wd._cycle(s, now=1001.0)          # 刚重启完，还在退避里
        wd._trigger_restart.assert_called_once()

    def test_gap_doubles_then_caps(self):
        """间隔倍增、封顶 —— 否则整夜没人说话会变成反复杀 QQ。"""
        wd._silent_seconds.return_value = wd.SILENCE_SECONDS + 1
        s = self.new_state()
        t = 1000.0
        gaps = []
        for _ in range(8):
            wd._cycle(s, now=t)
            gaps.append(s["silence_gap"])
            t = s["silence_until"] + 1
        self.assertEqual(gaps[0], wd.SILENCE_COOLDOWN * 2)
        self.assertEqual(gaps[-1], wd.SILENCE_MAX_GAP)

    def test_activity_resets_gap(self):
        """一有消息就把退避清零，别让上一轮的长间隔耽误下一次真故障。"""
        s = self.new_state()
        s["silence_gap"] = 99999.0
        wd._silent_seconds.return_value = 5.0
        wd._cycle(s, now=1000.0)
        self.assertEqual(s["silence_gap"], 0.0)

    def test_unreadable_snapshot_does_nothing(self):
        """读不到快照就不下判断 —— 那是进程级的毛病，交给探活分支。"""
        wd._silent_seconds.return_value = None
        s = self.new_state()
        wd._cycle(s, now=1000.0)
        wd._trigger_restart.assert_not_called()


class SilentSecondsTest(TestCase):
    """_silent_seconds：用快照的 ts 反推绝对时刻，别信会冻住的 last_activity_ago。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, True))
        self.path = self.dir + "/qq_status.json"

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def _call(self, now):
        with mock.patch.object(wd, "_STATUS_PATH", self.path):
            return wd._silent_seconds(now)

    def test_uses_ts_not_frozen_ago(self):
        # last_activity 的真实时刻 = ts - ago = 1000 - 3 = 997
        self._write('{"ts": 1000.0, "last_activity_ago": 3.0}')
        self.assertAlmostEqual(self._call(1100.0), 103.0, places=1)

    def test_missing_file(self):
        self.assertIsNone(self._call(1000.0))

    def test_stale_snapshot_is_unknown(self):
        """快照自己太旧 = qq_bot 的状态线程也停了 → 不下判断。"""
        self._write('{"ts": 1000.0, "last_activity_ago": 0.0}')
        self.assertIsNone(self._call(1000.0 + wd.STATUS_STALE + 1))

    def test_missing_field_is_unknown(self):
        self._write('{"ts": 1000.0}')
        self.assertIsNone(self._call(1000.0))

    def test_garbage_json(self):
        self._write("{not json")
        self.assertIsNone(self._call(1000.0))


class AwaitOutcomeTest(TestCase):
    """_await_outcome 的三态判定：三条件缺一不可。"""

    def setUp(self):
        p = mock.patch.object(wd, "_probe", return_value=("dead", "x"))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(wd, "_qr_written_since", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        # 假时钟：time.time() 每次往后跳 RECOVER_INTERVAL，循环能跑完不真等
        p = mock.patch.object(wd, "time")
        clock = p.start()
        self.addCleanup(p.stop)
        clock.time.side_effect = _FakeClock()
        clock.sleep = lambda s: None

    def test_online_wins(self):
        wd._probe.return_value = ("online", "ok")
        self.assertEqual(wd._await_outcome(0.0), "online")

    def test_qr_requires_grace_and_fresh_file(self):
        wd._probe.return_value = ("offline", "")
        wd._qr_written_since.return_value = True
        self.assertEqual(wd._await_outcome(0.0), "qr")

    def test_qr_not_declared_without_fresh_file(self):
        wd._probe.return_value = ("offline", "")
        wd._qr_written_since.return_value = False
        self.assertEqual(wd._await_outcome(0.0), "unknown")

    def test_dead_is_not_qr(self):
        """进程都没起来 → 不是「要扫码」。"""
        wd._probe.return_value = ("dead", "x")
        wd._qr_written_since.return_value = True
        self.assertEqual(wd._await_outcome(0.0), "unknown")


class _FakeClock:
    """每次调用往后跳 RECOVER_INTERVAL，让 _await_outcome 的循环能跑完。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += wd.RECOVER_INTERVAL
        return self.t
