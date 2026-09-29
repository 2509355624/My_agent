"""看门狗逻辑测试：只驱动 _cycle，把网络探测 / 重启 / 推送全 mock 掉。

覆盖：未到阈值不重启、到阈值触发重启并恢复、重启后要扫码只推一条合并通知、
重启连续失败放弃并退避、退避期内不动作。不涉及真实 NapCat / subprocess。
"""

from unittest import TestCase, mock

from app import watchdog as wd


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
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    @staticmethod
    def new_state():
        return {"fails": 0, "restart_fails": 0, "backoff_until": 0.0}

    def test_below_threshold_no_restart(self):
        wd._probe.side_effect = [(False, "x"), (False, "x"), (True, "ok")]
        s = self.new_state()
        wd._cycle(s, now=0.0)   # fail 1
        wd._cycle(s, now=1.0)   # fail 2
        wd._cycle(s, now=2.0)   # ok -> reset
        self.assertEqual(s["fails"], 0)
        wd._trigger_restart.assert_not_called()

    def test_threshold_triggers_restart_then_recovers(self):
        wd._probe.side_effect = [(False, "x"), (False, "x"), (False, "x"), (True, "ok")]
        s = self.new_state()
        for t in range(3):              # 连续 3 次失败
            wd._cycle(s, now=float(t))
        wd._trigger_restart.assert_called_once()
        wd._await_outcome.assert_called_once()
        wd._cycle(s, now=10.0)          # 恢复在线
        wd.push_text.assert_any_call("QQ 机器人已自动恢复", wd._RECOVER_TEXT)

    def test_no_premature_restart_message(self):
        """重启一发起不先发「正在自动重启」——只发结论那一条。"""
        wd._probe.return_value = (False, "x")
        s = self.new_state()
        for t in range(3):
            wd._cycle(s, now=float(t))
        titles = [c.args[0] for c in wd.push_text.call_args_list]
        self.assertNotIn("QQ 机器人正在自动重启", titles)

    def test_qr_outcome_sends_one_merged_notification(self):
        """重启后要扫码：只推一条带二维码的通知，不再另发「已恢复」。"""
        wd._await_outcome.return_value = "qr"
        wd._probe.return_value = (False, "x")
        s = self.new_state()
        for t in range(3):
            wd._cycle(s, now=float(t))
        wd.notify.push_offline.assert_called_once()
        self.assertIn("扫码", wd.notify.push_offline.call_args.kwargs["title"])
        self.assertTrue(wd.notify.push_offline.call_args.kwargs["intro"])
        self.assertEqual(wd.push_text.call_count, 0)
        self.assertEqual(s["restart_fails"], 0)

    def test_restart_flag_handed_over_and_released(self):
        """重启窗口期把二维码通知权接管过来，无论结果如何都要还回去。"""
        wd._probe.return_value = (False, "x")
        s = self.new_state()
        for t in range(3):
            wd._cycle(s, now=float(t))
        wd.notify.mark_restarting.assert_called_once()
        wd.notify.clear_restarting.assert_called_once()

    def test_restart_flag_released_when_trigger_fails(self):
        wd._trigger_restart.return_value = False
        wd._probe.return_value = (False, "x")
        s = self.new_state()
        for t in range(3):
            wd._cycle(s, now=float(t))
        wd.notify.clear_restarting.assert_called_once()
        wd._await_outcome.assert_not_called()

    def test_give_up_after_max_restart_failures(self):
        wd._probe.return_value = (False, "x")   # 永远失败
        wd._await_outcome.return_value = "unknown"   # 重启也救不活
        s = self.new_state()
        for t in range(9):               # 3 轮失败 = 3 次触发
            wd._cycle(s, now=float(t))
        self.assertEqual(wd._trigger_restart.call_count, wd.MAX_RESTART_FAILS)
        self.assertGreater(s["backoff_until"], 0.0)
        wd.push_text.assert_any_call(
            "QQ 机器人需人工处理",
            "看门狗多次自动重启失败，NapCat 仍无法恢复，请检查。")

    def test_backoff_suppresses_action(self):
        wd._probe.return_value = (False, "x")
        s = self.new_state()
        s["backoff_until"] = 1000.0      # 退避中
        wd._cycle(s, now=500.0)
        wd._trigger_restart.assert_not_called()

    def test_recovers_without_trigger_after_backoff(self):
        # 退避期过后、还没到失败阈值时自己恢复了
        wd._probe.side_effect = [(False, "x"), (True, "ok")]
        s = self.new_state()
        s["backoff_until"] = 0.0
        s["restart_fails"] = 1           # 上一轮重启失败过
        wd._cycle(s, now=0.0)            # fail 1（未到阈值，不重启）
        wd._cycle(s, now=1.0)            # 恢复
        wd._trigger_restart.assert_not_called()
        self.assertEqual(s["restart_fails"], 0)
        wd.push_text.assert_any_call("QQ 机器人已自动恢复", wd._RECOVER_TEXT)


class AwaitOutcomeTest(TestCase):
    """_await_outcome 的三态判定：三条件缺一不可。"""

    def setUp(self):
        p = mock.patch.object(wd, "_probe", return_value=(False, "x"))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(wd, "_port_open", return_value=False)
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
        wd._probe.return_value = (True, "ok")
        self.assertEqual(wd._await_outcome(0.0), "online")

    def test_qr_requires_port_and_grace_and_file(self):
        wd._port_open.return_value = True
        wd._qr_written_since.return_value = True
        self.assertEqual(wd._await_outcome(0.0), "qr")

    def test_qr_not_declared_without_fresh_file(self):
        wd._port_open.return_value = True
        wd._qr_written_since.return_value = False
        self.assertEqual(wd._await_outcome(0.0), "unknown")

    def test_qr_not_declared_when_webui_down(self):
        wd._port_open.return_value = False
        wd._qr_written_since.return_value = True
        self.assertEqual(wd._await_outcome(0.0), "unknown")


class _FakeClock:
    """每次调用往后跳 RECOVER_INTERVAL，让 _await_outcome 的循环能跑完。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += wd.RECOVER_INTERVAL
        return self.t
