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


if __name__ == "__main__":
    unittest.main()
