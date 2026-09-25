# -*- coding: utf-8 -*-
"""发送日志：每条实际发出的 QQ 消息都要在适配层终端打一行「发给了谁」。

用户诉求：NapCat 窗口看得到收发，适配层窗口只有接收没有发送，
排查「AI 到底回给了谁」得两边对。修法=在 qq_api 的发送必经之路上
打日志，带群名/昵称和内容预览；拿不到名字退回号码，不拦发送。
"""

import unittest
from unittest import mock

from app import qq_api


def _reset_names():
    """每个用例独享名字缓存，避免互相污染（模块级状态）。"""
    qq_api._names.clear()
    qq_api._names_fetched_at = 0.0


class _Base(unittest.TestCase):
    def setUp(self):
        _reset_names()
        self.addCleanup(_reset_names)
        # 所有 API 调用（发送 + 取名单）全部 mock 掉，不碰网络
        p = mock.patch.object(qq_api, "_call", return_value=None)
        self.api_call = p.start()
        self.addCleanup(p.stop)

    def groups(self, items):
        p = mock.patch.object(qq_api, "get_group_list", return_value=items)
        p.start()
        self.addCleanup(p.stop)

    def friends(self, items):
        p = mock.patch.object(qq_api, "get_friend_list", return_value=items)
        p.start()
        self.addCleanup(p.stop)


class GroupSendLogTest(_Base):
    def test_group_send_logs_name_id_and_text(self):
        self.groups([{"group_id": 1041079621, "group_name": "ai绘图交流"}])
        with self.assertLogs("qq_api", level="INFO") as cm:
            qq_api.send_group(1041079621, "大家好呀")
        line = "\n".join(cm.output)
        self.assertIn("发送 -> 群聊 [ai绘图交流(1041079621)]", line)
        self.assertIn("大家好呀", line)

    def test_group_send_without_name_shows_id(self):
        # 名单拿不到（NapCat 没回）也要照发照打日志，显示号码兜底
        self.groups([])
        with self.assertLogs("qq_api", level="INFO") as cm:
            qq_api.send_group(111, "在吗")
        line = "\n".join(cm.output)
        self.assertIn("发送 -> 群聊 [111]", line)
        self.assertIn("在吗", line)

    def test_split_message_logs_each_chunk(self):
        self.groups([{"group_id": 1, "group_name": "g"}])
        long = "很长的第一段。\n\n很长的第二段。" * 200
        with mock.patch.object(qq_api.time, "sleep"), \
                self.assertLogs("qq_api", level="INFO") as cm:
            n = qq_api.send_group(1, long, limit=50)
        self.assertGreater(n, 1)
        self.assertEqual(len(cm.output), n)   # 每个实际发出的分段一行


class PrivateSendLogTest(_Base):
    def test_private_send_logs_nickname(self):
        self.friends([{"user_id": 2308497189, "nickname": "洛琪希"}])
        with self.assertLogs("qq_api", level="INFO") as cm:
            qq_api.send_private(2308497189, "晚上好")
        line = "\n".join(cm.output)
        self.assertIn("发送 -> 私聊 [洛琪希(2308497189)]", line)
        self.assertIn("晚上好", line)

    def test_private_send_without_name_shows_id(self):
        self.friends([])
        with self.assertLogs("qq_api", level="INFO") as cm:
            qq_api.send_private(123, "嗨")
        self.assertIn("发送 -> 私聊 [123]", "\n".join(cm.output))


class ImageSendLogTest(_Base):
    def test_image_send_shows_placeholder_and_caption(self):
        self.groups([{"group_id": 2, "group_name": "g2"}])
        with self.assertLogs("qq_api", level="INFO") as cm:
            qq_api.send_image("group", 2, "D:/x/out.png", caption="画好了")
        line = "\n".join(cm.output)
        self.assertIn("发送 -> 群聊 [g2(2)]", line)
        self.assertIn("画好了", line)
        self.assertIn("[图片]", line)

    def test_image_send_without_caption_still_logged(self):
        self.groups([])
        with self.assertLogs("qq_api", level="INFO") as cm:
            qq_api.send_image("group", 2, "D:/x/out.png")
        self.assertIn("[图片]", "\n".join(cm.output))


class NameCacheTest(_Base):
    def test_name_list_fetched_once_per_process(self):
        self.groups([{"group_id": 3, "group_name": "g3"}])
        with self.assertLogs("qq_api", level="INFO"):
            qq_api.send_group(3, "一")
            qq_api.send_group(3, "二")
        # 拉名单只在第一次发送时发生（get_group_list 走 mock，_call 只收发送）
        send_calls = [c for c in self.api_call.call_args_list
                      if c.args[0] == "send_group_msg"]
        self.assertEqual(len(send_calls), 2)

    def test_failed_name_lookup_is_retried_later_not_every_send(self):
        # 名单接口挂了不该每发一条就白等一次超时——失败后 10 分钟内直接用号码
        self.groups([])
        with self.assertLogs("qq_api", level="INFO"):
            qq_api.send_group(9, "a")
            qq_api.send_group(9, "b")
        # get_group_list 只在第一条消息时被调过一次
        with mock.patch.object(qq_api, "get_group_list",
                               return_value=[]) as g:
            qq_api.send_group(9, "c")
        g.assert_not_called()


if __name__ == "__main__":
    unittest.main()
