"""send_sticker 工具测试：轮次硬闸 + 频率闸 + 编号解析 + 发送汇总。

原则：零网络、零真实 agents 目录。库存与发送端全 mock，只测工具本身
的判定逻辑（两道闸 / 无效编号 / 目标解析）。
"""

import unittest
from unittest import mock

from app.tools.normal import send_sticker


def _rec(num=1):
    return (num, {"desc": "猫瘫在桌上打滚", "file": "a.png", "tags": ["摆烂"]})


class _Base(unittest.TestCase):
    """公共脚手架：清状态 + 挡掉 current_context / 轮次 / 库存 / 发送。"""

    def setUp(self):
        # 频率闸和轮次额度都是模块级状态，用例间必须清干净
        send_sticker._send_state.clear()
        send_sticker._turn_count.clear()
        self.now = [1000.0]
        p = mock.patch.object(send_sticker.time, "monotonic",
                              lambda: self.now[0])
        p.start()
        self.addCleanup(p.stop)
        # 轮次编号可控：默认全程同一轮；要模拟「换轮」就 self.turn[0] += 1
        self.turn = [0]
        p = mock.patch.object(send_sticker.qq_api, "current_turn_id",
                              lambda: self.turn[0])
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

    def _bump(self, n):
        """隔开频率闸连发 n 次（每次一张），返回每次的返回话。"""
        out = []
        for _ in range(n):
            out.append(self._call("1"))
            self.now[0] += send_sticker.STICKER_MIN_INTERVAL + 0.5
        return out


class TurnCapTest(_Base):
    """本轮张数硬闸：到量即终止，换轮自动归零。

    这道闸是 2026-09-29 为「被子教」群那个死循环加的：一轮里模型反复调
    send_sticker，而循环只在「回纯文本且不带工具」时才结束，于是它一路甩到
    MAX_TURNS(20)，群里先连蹦十几张图、最后才掉下一条千字大文本。
    """

    def test_cap_blocks_after_limit(self):
        cap = send_sticker.STICKER_MAX_PER_TURN
        for r in self._bump(cap):
            self.assertIn("已发表情包", r)
        blocked = self._call("1")
        self.assertIn("已经发够", blocked)
        self.assertIn("不要再调 send_sticker", blocked)
        self.assertEqual(self.send.call_count, cap)   # 第 cap+1 次没真发

    def test_cap_message_does_not_invite_another_try(self):
        """挡回来的话里不能有让模型「再来一轮」的指示。

        原来那句「先用文字接一句，**下条立刻补图**」正是死循环的放大器——
        它把每一次被闸都变成下一次迭代的理由。这条钉住措辞不再回潮。
        """
        self._bump(send_sticker.STICKER_MAX_PER_TURN)
        blocked = self._call("1")
        for bad in ("下条", "补图", "再试一次", "隔 "):
            self.assertNotIn(bad, blocked)

    def test_throttle_message_also_terminates(self):
        self._call("1")
        blocked = self._call("1")
        self.assertIn("太密", blocked)
        self.assertIn("别再调 send_sticker", blocked)
        self.assertNotIn("补图", blocked)

    def test_new_turn_resets_quota(self):
        cap = send_sticker.STICKER_MAX_PER_TURN
        self._bump(cap)
        self.assertIn("已经发够", self._call("1"))
        self.turn[0] += 1                      # 换轮 → 额度归零
        self.assertIn("已发表情包", self._call("1"))

    def test_quota_counts_attempts_not_successes(self):
        """发送一直失败也不能无限重试——额度按「尝试数」扣。"""
        self.send.side_effect = RuntimeError("断了")
        self._bump(send_sticker.STICKER_MAX_PER_TURN)
        self.assertIn("已经发够", self._call("1"))

    def test_targets_have_separate_quota(self):
        self._bump(send_sticker.STICKER_MAX_PER_TURN)
        with mock.patch.object(send_sticker.qq_api, "current_context",
                               return_value=("group", "10")):
            self.assertIn("已发表情包", self._call("1"))

    def test_invalid_nums_do_not_consume_quota(self):
        self._bump(send_sticker.STICKER_MAX_PER_TURN)
        with mock.patch.object(send_sticker.stickers,
                               "records_by_numbers", return_value=[]):
            self.assertIn("没认出", self._call("99"))
        self.assertIn("已经发够", self._call("1"))


class ThrottleTest(_Base):
    """频率闸：太密挡回、到点放行、各会话独立。（间隔值取自常量，不写死）"""

    def test_second_call_within_interval_is_blocked(self):
        self.assertIn("已发表情包", self._call("1"))
        blocked = self._call("1")
        self.assertIn("太密", blocked)
        self.assertIn("%g" % send_sticker.STICKER_MIN_INTERVAL, blocked)
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
        # 报错号不该白白吃掉一次间隔
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
        # 一条消息、一个图片段：外层是「消息列表」，里层才是段列表
        msg = self.send.call_args[0][1]
        self.assertEqual(len(msg), 1)
        seg = msg[0][0]
        self.assertEqual(seg["type"], "image")
        self.assertTrue(seg["data"]["file"].startswith("file:///"))
        self.assertTrue(seg["data"]["file"].endswith("/a.png"))

    def test_one_segment_is_one_message_not_one_dict(self):
        """必须传 `[[seg]]`：外层少套一层会炸在日志预览上。

        send_group / send_private 收到 list 是按「多条消息」解释的（每元素
        一条）。只传 `[seg]` 等于说"这条消息是一个 dict"，_send_log → _preview
        去遍历它，拿到的是键名（字符串）→ 'str' object has no attribute 'get'。
        实测图其实已经发出去了，工具却报失败还中断了后面几张。
        """
        self._call("1")
        msg = self.send.call_args[0][1]
        self.assertIsInstance(msg, list)
        self.assertEqual(len(msg), 1)             # 一条消息
        self.assertIsInstance(msg[0], list)       # 消息体是段列表
        self.assertIsInstance(msg[0][0], dict)    # 里面那个才是段


if __name__ == "__main__":
    unittest.main()
