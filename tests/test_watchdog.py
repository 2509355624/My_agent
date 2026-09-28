"""看门狗逻辑测试：只驱动 _cycle，把网络探测 / 重启 / 推送全 mock 掉。

覆盖：未到阈值不重启、到阈值触发重启并恢复、重启连续失败放弃并退避、
退避期内不动作。不涉及真实 NapCat / subprocess。
"""

from unittest import TestCase, mock

from app import watchdog as wd


class WatchdogCycleTest(TestCase):

    def setUp(self):
        self.patchers = [
            mock.patch.object(wd, "_acquire_lock", lambda: None),
            mock.patch.object(wd, "_probe"),
            mock.patch.object(wd, "_trigger_restart", return_value=True),
            mock.patch.object(wd, "_recovering", return_value=True),
            mock.patch.object(wd, "push_text", return_value=(True, "ok")),
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
        wd._recovering.assert_called_once()
        wd._cycle(s, now=10.0)          # 恢复在线
        wd.push_text.assert_any_call(
            "QQ 机器人已自动恢复", "NapCat 重启后已自动上线。")

    def test_give_up_after_max_restart_failures(self):
        wd._probe.return_value = (False, "x")   # 永远失败
        wd._recovering.return_value = False     # 重启也救不活
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
        wd.push_text.assert_any_call(
            "QQ 机器人已自动恢复", "NapCat 重启后已自动上线。")
