"""qq_api._call 对非对象响应的容错 + records_by_numbers 兜底。

背景（2026-09-26 凌晨 233 群实录）：send_sticker 已把图发出去，NapCat 返回的
响应体 json() 解析成了字符串，_call 里 data.get 崩掉、上层报「发送失败」——
模型于是以为图没发出去，下文接着编。修复：POST 完成后的非对象响应按成功处理。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, ".")

from app import qq_api, stickers  # noqa: E402


def _resp(json_value, raise_json_error=False):
    r = mock.Mock()
    r.status_code = 200
    r.text = "原始报文"
    if raise_json_error:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = json_value
    return r


class CallResponseTest(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()
        p = mock.patch.object(qq_api, "_session", self.session)
        p.start()
        self.addCleanup(p.stop)

    def test_string_response_treated_as_success(self):
        """非对象响应（实测偶发）：POST 已完成，按成功处理不抛错。"""
        self.session.post.return_value = _resp("奇怪的字符串")
        self.assertEqual(qq_api._call("send_group_msg", {}), {})

    def test_dict_response_returns_data(self):
        self.session.post.return_value = _resp(
            {"status": "ok", "retcode": 0, "data": {"message_id": 7}})
        self.assertEqual(qq_api._call("send_group_msg", {}), {"message_id": 7})

    def test_failed_status_still_raises(self):
        self.session.post.return_value = _resp(
            {"status": "failed", "retcode": 1200, "message": "bad"})
        with self.assertRaises(RuntimeError):
            qq_api._call("send_group_msg", {})

    def test_non_json_body_still_raises(self):
        self.session.post.return_value = _resp(None, raise_json_error=True)
        with self.assertRaises(RuntimeError):
            qq_api._call("send_group_msg", {})


class RecordsByNumbersTest(unittest.TestCase):
    def test_skips_non_dict_entries(self):
        tmp = tempfile.mkdtemp()
        real_file = os.path.join(tmp, "x.gif")
        with open(real_file, "wb") as f:
            f.write(b"x")
        p1 = mock.patch.object(stickers, "_dir", return_value=tmp)
        p2 = mock.patch.object(stickers, "_load_index",
                               return_value=["坏行", {"file": "x.gif"}])
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        picks = stickers.records_by_numbers("qq", "1,2")
        self.assertEqual([n for n, _ in picks], [2])


class SingleSegmentMessageTest(unittest.TestCase):
    """发「一条消息、里面一个图片段」时整条链路要能走通（含日志预览那层）。

    背景（2026-09-26 21:53 群实录）：send_sticker 传的是 `[seg]` 而不是
    `[[seg]]`，而 send_group 收到 list 是按「多条消息」解释的——于是那个 seg
    被当成"整条消息"，_send_log → _preview 去遍历它，拿到的是键名（字符串）
    → 'str' object has no attribute 'get'。图其实已经发出去了（_call 在前），
    工具却报「发送失败」，还中断了后面几张。

    与上面那次是同一个症状的第二个成因，所以两处都钉：调用方套对层数，
    _preview 自己也吞得下裸 dict（预览只是日志，不该有能力把发送弄挂）。
    """

    def setUp(self):
        self.session = mock.Mock()
        for name, value in (("_session", self.session),
                            ("_display_name", mock.Mock(return_value="测试群"))):
            p = mock.patch.object(qq_api, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_preview_tolerates_bare_segment(self):
        seg = {"type": "image", "data": {"file": "file:///x.gif"}}
        self.assertEqual(qq_api._preview(seg), "[图片]")

    def test_single_segment_message_sends_and_logs(self):
        self.session.post.return_value = _resp(
            {"status": "ok", "retcode": 0, "data": {}})
        seg = {"type": "image", "data": {"file": "file:///x.gif"}}
        qq_api.send_group(123, [[seg]])              # 从前这里会抛
        sent = self.session.post.call_args[1]["json"]["message"]
        self.assertEqual(sent, [seg])


class TempSessionFallbackTest(unittest.TestCase):
    """非好友私聊：被 QQ 拒了就用**群临时会话**重发。

    2026-09-30 实测（胡桃桃 3985441738 私聊小小怪）：机器人正常跑完一整轮，卡在
    最后一步投递——QQ 不允许给非好友发私聊。但对方是从 **233的粉丝群（1103174141）**
    发起的**临时会话**，带上那个群的 group_id 就发出去了。

    两条反直觉的事实（都是实测出来的，别想当然）：
    1. 临时会话**按群存在**——共同群有 7 个，只有 1103174141 通，其余都报
       `no such temp session`；
    2. **入站事件里不带 group_id**，所以「等对方发起后自动记」行不通，
       只能**挨个群试**（试错安全：没会话的群不会真把消息投出去）。
    """

    def setUp(self):
        qq_api._temp_group.clear()
        self.addCleanup(qq_api._temp_group.clear)
        qq_api._group_cache.update(ts=0.0, ids=[])
        self.addCleanup(qq_api._group_cache.update, ts=0.0, ids=[])
        self.session = mock.Mock()
        p = mock.patch.object(qq_api, "_session", self.session)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(qq_api, "_send_log")
        p.start()
        self.addCleanup(p.stop)
        # 机器人所在的群：探测就是在这几个群里挨个试
        p = mock.patch.object(qq_api, "get_group_list",
                              return_value=[{"group_id": "1041446471"},
                                            {"group_id": "1103174141"}])
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _ok():
        return _resp({"status": "ok", "retcode": 0, "data": {"message_id": 1}})

    @staticmethod
    def _not_friend():
        return _resp({"status": "failed", "retcode": 100, "data": None,
                      "wording": "send private message rejected: result=16 "
                                 "err=发送失败，请先添加对方为好友"})

    @staticmethod
    def _no_session():
        return _resp({"status": "failed", "retcode": 100, "data": None,
                      "wording": "cannot send to user 1 in group 2: "
                                 "no such temp session"})

    def _payloads(self):
        return [c.kwargs.get("json") for c in self.session.post.call_args_list]

    def test_plain_send_uses_no_group_id(self):
        self.session.post.return_value = self._ok()
        qq_api.send_private("3985441738", "你好")
        self.assertNotIn("group_id", self._payloads()[0])

    def test_uses_known_temp_session_directly(self):
        qq_api.note_temp_session("3985441738", "1103174141")
        self.session.post.side_effect = [self._not_friend(), self._ok()]
        self.assertEqual(qq_api.send_private("3985441738", "你好"), 1)
        sent = self._payloads()
        self.assertEqual(len(sent), 2)          # 没有多余的探测
        self.assertNotIn("group_id", sent[0])
        self.assertEqual(sent[1]["group_id"], 1103174141)
        self.assertEqual(sent[1]["user_id"], 3985441738)

    def test_probes_every_group_when_the_group_is_unknown(self):
        """入站事件不带 group_id，所以不知道是哪个群——只能挨个试。"""
        self.session.post.side_effect = [self._not_friend(),   # 普通私聊被拒
                                         self._no_session(),   # 群 1041446471 没会话
                                         self._ok()]           # 群 1103174141 通
        self.assertEqual(qq_api.send_private("3985441738", "你好"), 1)
        sent = self._payloads()
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[1]["group_id"], 1041446471)
        self.assertEqual(sent[2]["group_id"], 1103174141)

    def test_probe_remembers_the_winner_so_next_time_is_direct(self):
        self.session.post.side_effect = [self._not_friend(), self._no_session(),
                                         self._ok()]
        qq_api.send_private("3985441738", "第一条")
        self.assertEqual(qq_api.temp_group_of("3985441738"), "1103174141")
        # 第二次：一次普通私聊 + 一次带已知群的发送，不该再探测
        self.session.post.reset_mock()
        self.session.post.side_effect = [self._not_friend(), self._ok()]
        qq_api.send_private("3985441738", "第二条")
        sent = self._payloads()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1]["group_id"], 1103174141)

    def test_probe_gives_up_and_raises_when_no_group_has_a_session(self):
        """所有群都没有活跃临时会话时，老实报错——不能假装发成功。"""
        self.session.post.side_effect = [self._not_friend(),
                                         self._no_session(), self._no_session()]
        with self.assertRaises(RuntimeError) as ctx:
            qq_api.send_private("3985441738", "你好")
        self.assertTrue(qq_api.friend_required_error(ctx.exception))
        self.assertIsNone(qq_api.temp_group_of("3985441738"))

    def test_probe_does_not_swallow_real_errors(self):
        """探测途中撞上真故障（风控/超时）要照抛，不能被当成「这个群没会话」。"""
        self.session.post.side_effect = [self._not_friend(),
                                         self._resp_rate_limited()]
        with self.assertRaises(RuntimeError) as ctx:
            qq_api.send_private("3985441738", "你好")
        self.assertNotIn("no such temp session", str(ctx.exception))

    @staticmethod
    def _resp_rate_limited():
        return _resp({"status": "failed", "retcode": 100, "data": None,
                      "wording": "频率过快，请稍后再试"})

    def test_other_errors_are_not_masked_by_the_fallback(self):
        """别的失败照抛，不能都算成「对方不是好友」——那会掩盖真故障。"""
        qq_api.note_temp_session("3985441738", "1103174141")
        self.session.post.return_value = self._resp_rate_limited()
        with self.assertRaises(RuntimeError):
            qq_api.send_private("3985441738", "你好")
        self.assertEqual(len(self.session.post.call_args_list), 1)

    def test_only_the_failed_chunk_is_retried(self):
        """分段发送：已发出去的段不能重发（对方会收到两遍）。"""
        qq_api.note_temp_session("3985441738", "1103174141")
        self.session.post.side_effect = [self._ok(), self._not_friend(), self._ok()]
        self.assertEqual(qq_api.send_private("3985441738", ["第一段", "第二段"]), 2)
        sent = self._payloads()
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[1]["message"], "第二段")
        self.assertNotIn("group_id", sent[1])
        self.assertEqual(sent[2]["message"], "第二段")
        self.assertEqual(sent[2]["group_id"], 1103174141)

    def test_private_image_also_falls_back(self):
        """图这条尤其要兜：对方看到「在画了」然后什么都没有更难受。"""
        qq_api.note_temp_session("3985441738", "1103174141")
        self.session.post.side_effect = [self._not_friend(), self._ok()]
        qq_api.send_image("private", "3985441738", "D:/x.png")
        sent = self._payloads()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1]["group_id"], 1103174141)

    def test_group_image_still_uses_plain_group_send(self):
        self.session.post.return_value = self._ok()
        qq_api.send_image("group", "1103174141", "D:/x.png")
        sent = self._payloads()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["group_id"], 1103174141)
        self.assertEqual(len(self.session.post.call_args_list), 1)

    def test_note_temp_session_ignores_empty_values(self):
        qq_api.note_temp_session("", "1103174141")
        qq_api.note_temp_session("3985441738", "")
        self.assertIsNone(qq_api.temp_group_of("3985441738"))

    def test_stale_remembered_group_falls_back_to_probing(self):
        """记住的群**过期了**要忘掉它并重新探测，不能拿它一直撞。

        2026-10-01 实测：23:5x 还通的 1103174141，过一会儿就回
        `no such temp session` —— 临时会话有有效期。老实现拿记住的群发，
        失败就直接抛，**永远不重新探测**，于是「时灵时不灵」。
        """
        qq_api.note_temp_session("3985441738", "1103174141")   # 记着一个已过期的群
        self.session.post.side_effect = [self._not_friend(),   # 普通私聊被拒
                                         self._no_session(),   # 记住的群：过期了
                                         self._no_session(),   # 探测 1041446471
                                         self._ok()]           # 探测 1103174141：通了
        self.assertEqual(qq_api.send_private("3985441738", "你好"), 1)
        sent = self._payloads()
        self.assertEqual(len(sent), 4)
        self.assertEqual(sent[1]["group_id"], 1103174141)      # 先试记住的
        self.assertEqual(sent[3]["group_id"], 1103174141)      # 探测后再发
        self.assertEqual(qq_api.temp_group_of("3985441738"), "1103174141")

    def test_stale_group_is_forgotten_when_probing_also_fails(self):
        """过期群 + 探测全失败 → 缓存清掉，抛出的仍是「非好友」这个真因。"""
        qq_api.note_temp_session("3985441738", "1103174141")
        self.session.post.side_effect = [self._not_friend(),
                                         self._no_session(),   # 记住的群过期
                                         self._no_session(),   # 探测 1041446471
                                         self._no_session()]   # 探测 1103174141
        with self.assertRaises(RuntimeError) as ctx:
            qq_api.send_private("3985441738", "你好")
        self.assertTrue(qq_api.friend_required_error(ctx.exception))
        self.assertIsNone(qq_api.temp_group_of("3985441738"))

    def test_real_errors_on_remembered_group_are_not_treated_as_expiry(self):
        """用记住的群发时撞上**别的**错（风控/频率限制）→ 照抛，缓存也别误删。"""
        qq_api.note_temp_session("3985441738", "1103174141")
        self.session.post.side_effect = [
            self._not_friend(),
            _resp({"status": "failed", "retcode": 100, "data": None,
                   "wording": "发送太快，请稍后再试"})]
        with self.assertRaises(RuntimeError) as ctx:
            qq_api.send_private("3985441738", "你好")
        self.assertFalse(qq_api.friend_required_error(ctx.exception))
        self.assertEqual(qq_api.temp_group_of("3985441738"), "1103174141")
        self.assertEqual(len(self._payloads()), 2)             # 没有多余探测

    def test_forget_temp_session_is_safe_when_absent(self):
        qq_api.forget_temp_session("3985441738")               # 没有也不该炸
        self.assertIsNone(qq_api.temp_group_of("3985441738"))


if __name__ == "__main__":
    unittest.main()
