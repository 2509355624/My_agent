"""send_sticker 工具测试：15 秒频率闸 + 编号解析 + 发送汇总。

原则：零网络、零真实 agents 目录。库存与发送端全 mock，只测工具本身
的判定逻辑（频率闸 / 无效编号 / 目标解析）。
"""

import unittest
from unittest import mock

from app.tools.normal import send_sticker


def _rec(num=1):
    return (num, {"desc": "猫瘫在桌上打滚", "file": "a.png", "tags": ["摆烂"]})


class _Base(unittest.TestCase):
    """公共脚手架：清状态 + 挡掉 current_context / 库存 / 发送。"""

    def setUp(self):
        # 频率闸是模块级状态，用例间必须清干净
        send_sticker._send_state.clear()
        self.now = [1000.0]
        p = mock.patch.object(send_sticker.time, "monotonic",
                              lambda: self.now[0])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(send_sticker.qq_api, "current_context",
                              return_value=("group", "9"))
        p.start()
        self.addCleanup(p.stop)
        self.send = mock.Mock()
        p = mock.patch.object(send_sticker.qq_api, "send_group", self.send)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(send_sticker.qq_api, "send_private")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(send_sticker.stickers, "records_by_numbers",
                              return_value=[_rec()])
        p.start()
        self.addCleanup(p.stop)

    def _call(self, nums="1"):
        return send_sticker.tool["function"](nums)


class ThrottleTest(_Base):
    """30 秒频率闸：太密挡回、到点放行、各会话独立。"""

    def test_second_call_within_interval_is_blocked(self):
        self.assertIn("已发表情包", self._call("1"))
        blocked = self._call("1")
        self.assertIn("太密", blocked)
        self.assertIn(str(int(send_sticker.STICKER_MIN_INTERVAL)), blocked)
        self.assertEqual(self.send.call_count, 1)   # 第二次没真发

    def test_call_after_interval_passes(self):
        self._call("1")
        self.now[0] += send_sticker.STICKER_MIN_INTERVAL + 0.5
        self.assertIn("已发表情包", self._call("1"))
        self.assertEqual(self.send.call_count, 2)

    def test_targets_are_independent(self):
        # group 9 刚发过，group 10 不受影响——群和群各算各的
        self._call("1")
        with mock.patch.object(send_sticker.qq_api, "current_context",
                               return_value=("group", "10")):
            self.assertIn("已发表情包", self._call("1"))

    def test_invalid_nums_do_not_consume_interval(self):
        # 报错号不该白白吃掉一次 30 秒间隔
        with mock.patch.object(send_sticker.stickers,
                               "records_by_numbers", return_value=[]):
            self.assertIn("没认出", self._call("99"))
        self.assertIn("已发表情包", self._call("1"))


class SendResultTest(_Base):
    """发送结果汇总：多张连发、单张失败。"""

    def test_multiple_numbers_sends_each(self):
        with mock.patch.object(send_sticker.stickers, "records_by_numbers",
                               return_value=[_rec(3), _rec(7)]):
            out = self._call("3,7")
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("3号", out)
        self.assertIn("7号", out)

    def test_send_failure_is_reported(self):
        self.send.side_effect = RuntimeError("断了")
        out = self._call("1")
        self.assertIn("发送失败", out)
        self.assertIn("断了", out)

    def test_sends_local_file_uri(self):
        self._call("1")
        seg = self.send.call_args[0][1][0]
        self.assertEqual(seg["type"], "image")
        self.assertTrue(seg["data"]["file"].startswith("file:///"))
        self.assertTrue(seg["data"]["file"].endswith("/a.png"))


if __name__ == "__main__":
    unittest.main()
