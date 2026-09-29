"""掉线通知（app/notify.py）测试。

全程 mock：不真发 PushPlus、不真碰 NapCat。二维码用临时文件模拟 ——「NapCat
刷新了二维码」在测试里就是「把文件的 mtime 往后挪」。

mtime 一律显式钉死（不用 time.time()），因为 Windows 上连续两次写文件的
时间戳可能落在同一刻度上，那样「文件变了」就测不出来。
"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from app import notify


def _post_capture(sink):
    """假 _post：记下 payload，返回 PushPlus 的成功响应。"""
    def _post(payload, timeout=20):
        sink.append(payload)
        return 200, '{"code":200,"data":"x","msg":"执行成功"}'
    return _post


def _post_fail(payload, timeout=20):
    return 200, '{"code":500,"msg":"失败"}'


class _TmpQR:
    """一个可随手改动的「二维码文件」。"""

    def __init__(self, case):
        d = tempfile.TemporaryDirectory()
        case.addCleanup(d.cleanup)
        self.dir = d.name
        self.path = os.path.join(d.name, "qrcode.png")
        self.touch(b"png-1")

    def touch(self, data=None, at=None):
        """写文件；at 给定时把 mtime 一并钉到那个时刻。"""
        with open(self.path, "wb") as f:
            f.write(data if data is not None else b"png")
        if at is not None:
            os.utime(self.path, (at, at))


class PushOfflineTest(unittest.TestCase):
    def setUp(self):
        self.qr = _TmpQR(self)
        for name, val in (("NOTIFY_PUSHPLUS_TOKEN", "tok-123"),
                          ("NOTIFY_QRCODE_PATH", self.qr.path)):
            p = mock.patch.object(notify, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.sent = []
        p = mock.patch.object(notify, "_post", _post_capture(self.sent))
        p.start()
        self.addCleanup(p.stop)

    def test_disabled_without_token(self):
        """没配 token 时一个请求都不该发出去。"""
        with mock.patch.object(notify, "NOTIFY_PUSHPLUS_TOKEN", ""):
            self.assertFalse(notify.enabled())
            ok, _ = notify.push_offline()
        self.assertFalse(ok)
        self.assertEqual(self.sent, [])

    def test_payload_shape(self):
        self.assertTrue(notify.push_offline()[0])
        p = self.sent[0]
        self.assertEqual(p["token"], "tok-123")
        self.assertEqual(p["template"], "html")
        self.assertTrue(p["title"])

    def test_default_path_used(self):
        """不带参数时读配置里的默认路径，且二维码以 base64 内嵌。"""
        notify.push_offline()
        self.assertIn("data:image/png;base64,", self.sent[0]["content"])

    def test_reason_in_body(self):
        notify.push_offline(reason_title="BotOfflineEvent", reason_desc="你已下线")
        body = self.sent[0]["content"]
        self.assertIn("BotOfflineEvent", body)
        self.assertIn("你已下线", body)

    def test_missing_file_still_notifies(self):
        """拿不到图也要把「掉线了」推出去，只是少一张图。"""
        ok, _ = notify.push_offline(os.path.join(self.qr.dir, "nope.png"))
        self.assertTrue(ok)
        self.assertIn("没拿到二维码文件", self.sent[0]["content"])

    def test_network_error_does_not_raise(self):
        with mock.patch.object(notify, "_post", side_effect=OSError("boom")):
            ok, info = notify.push_offline()
        self.assertFalse(ok)
        self.assertIn("boom", info)

    def test_api_error_returns_false(self):
        """HTTP 通了但业务码不是 200，同样算失败。"""
        with mock.patch.object(notify, "_post", _post_fail):
            self.assertFalse(notify.push_offline()[0])


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.qr = _TmpQR(self)
        flag_dir = tempfile.TemporaryDirectory()
        self.addCleanup(flag_dir.cleanup)
        for name, val in (("NOTIFY_PUSHPLUS_TOKEN", "tok"),
                          ("NOTIFY_QRCODE_PATH", self.qr.path),
                          ("_RESTART_FLAG",
                           os.path.join(flag_dir.name, "restart.flag"))):
            p = mock.patch.object(notify, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(notify, "_online", lambda: False)
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        p = mock.patch.object(notify, "_post", _post_capture(self.sent))
        p.start()
        self.addCleanup(p.stop)
        notify.note_offline_reason("", "")      # 清掉上个用例留下的原因

    def _watcher(self, cooldown=notify._COOLDOWN_SECONDS):
        return notify._Watcher(self.qr.path, cooldown=cooldown)

    def test_startup_ignores_stale_file(self):
        """启动时文件是上一轮的遗留 → 不补推。"""
        self.qr.touch(at=time.time() - 3600)
        self.assertFalse(self._watcher().prime())
        self.assertEqual(self.sent, [])

    def test_startup_pushes_when_fresh_and_offline(self):
        """文件很新 + 不在线 = 进程重启前就掉了还没人扫，补一条。"""
        self.assertTrue(self._watcher().prime())
        self.assertEqual(len(self.sent), 1)

    def test_startup_silent_when_online(self):
        """文件很新但探活通（快速登录已自愈）→ 不推。"""
        with mock.patch.object(notify, "_online", lambda: True):
            self.assertFalse(self._watcher().prime())
        self.assertEqual(self.sent, [])

    def test_tick_pushes_on_new_qrcode(self):
        w = self._watcher()
        self.qr.touch(b"png-2", at=w.last_mtime + 10)
        self.assertTrue(w.tick())
        self.assertEqual(len(self.sent), 1)

    def test_tick_ignores_unchanged(self):
        w = self._watcher()
        self.assertFalse(w.tick())
        self.assertFalse(w.tick())
        self.assertEqual(self.sent, [])

    def test_cooldown_suppresses_repeat(self):
        """NapCat 会重写同一个二维码文件，不该把人轰炸一遍。"""
        w = self._watcher(cooldown=300)
        base = w.last_mtime
        self.qr.touch(b"png-2", at=base + 10)
        self.assertTrue(w.tick(now=1000))
        self.qr.touch(b"png-3", at=base + 20)
        self.assertFalse(w.tick(now=1100))
        self.assertEqual(len(self.sent), 1)
        self.qr.touch(b"png-4", at=base + 30)     # 过了冷却，照常推
        self.assertTrue(w.tick(now=2000))
        self.assertEqual(len(self.sent), 2)

    def test_failed_push_does_not_start_cooldown(self):
        """推送失败要能重试 —— 否则一次网络抖动就静默丢掉整次掉线。"""
        w = self._watcher(cooldown=300)
        base = w.last_mtime
        with mock.patch.object(notify, "_post", _post_fail):
            self.qr.touch(b"png-2", at=base + 10)
            self.assertFalse(w.tick(now=1000))
        self.qr.touch(b"png-3", at=base + 20)
        self.assertTrue(w.tick(now=1010))

    def test_notice_reason_rides_along(self):
        notify.note_offline_reason("BotOfflineEvent", "你已下线")
        w = self._watcher()
        self.qr.touch(b"png-2", at=w.last_mtime + 10)
        w.tick()
        self.assertIn("你已下线", self.sent[0]["content"])

    def test_reason_consumed_once(self):
        """原因只跟着第一条走，不在后续通知里复读。"""
        notify.note_offline_reason("T", "D")
        w = self._watcher(cooldown=0)
        base = w.last_mtime
        self.qr.touch(b"png-2", at=base + 10)
        w.tick(now=1000)
        self.qr.touch(b"png-3", at=base + 20)
        w.tick(now=2000)
        self.assertIn("D", self.sent[0]["content"])
        self.assertNotIn("D", self.sent[1]["content"])


class StartWatcherTest(unittest.TestCase):
    def test_no_thread_without_token(self):
        with mock.patch.object(notify, "NOTIFY_PUSHPLUS_TOKEN", ""):
            with mock.patch.object(notify.threading, "Thread") as th:
                notify.start_watcher()
        th.assert_not_called()

    def test_thread_started_with_token(self):
        with mock.patch.object(notify, "NOTIFY_PUSHPLUS_TOKEN", "tok"):
            with mock.patch.object(notify, "_started", False):
                with mock.patch.object(notify.threading, "Thread") as th:
                    notify.start_watcher()
        th.assert_called_once()
        self.assertTrue(th.call_args.kwargs.get("daemon"))


class DispatchOfflineNoticeTest(unittest.TestCase):
    """qq_bot 收到 bot_offline notice 时要把原因留下来。"""

    def setUp(self):
        from app import qq_bot
        self.bot = qq_bot.QQBot()
        notify.note_offline_reason("", "")

    def _send(self, ev):
        self.bot._dispatch(json.dumps(ev))

    def test_bot_offline_records_reason(self):
        self._send({"post_type": "notice", "notice_type": "bot_offline",
                    "self_id": 3887072541, "tag": "BotOfflineEvent",
                    "message": "你已下线"})
        self.assertEqual(notify._take_reason(), ("BotOfflineEvent", "你已下线"))

    def test_other_notice_ignored(self):
        self._send({"post_type": "notice", "notice_type": "group_recall",
                    "group_id": 123})
        self.assertEqual(notify._take_reason(), ("", ""))

    def test_meta_event_ignored(self):
        self._send({"post_type": "meta_event", "meta_event_type": "heartbeat"})
        self.assertEqual(notify._take_reason(), ("", ""))

    def test_bad_payload_does_not_raise(self):
        self.bot._dispatch("{not json")
        self.bot._dispatch("[]")
        self.assertEqual(notify._take_reason(), ("", ""))


class RestartHandoffTest(unittest.TestCase):
    """看门狗自动重启期间由看门狗统一发通知，watcher 必须让位。

    否则同一次掉线会收到两条：看门狗的「已重启 + 要扫码」和 watcher 的
    「掉线了，需要扫码」（2026-09-29 用户抱怨）。
    """

    def setUp(self):
        self.qr = _TmpQR(self)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.flag = os.path.join(d.name, "restart.flag")
        for name, val in (("NOTIFY_PUSHPLUS_TOKEN", "tok"),
                          ("NOTIFY_QRCODE_PATH", self.qr.path),
                          ("_RESTART_FLAG", self.flag)):
            p = mock.patch.object(notify, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(notify, "_online", lambda: False)
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        p = mock.patch.object(notify, "_post", _post_capture(self.sent))
        p.start()
        self.addCleanup(p.stop)

    def _flag_on(self, age=0.0):
        with open(self.flag, "w", encoding="utf-8") as f:
            f.write("x")
        if age:
            os.utime(self.flag, (time.time() - age, time.time() - age))

    def test_mark_and_clear_roundtrip(self):
        self.assertFalse(notify.restarting())
        notify.mark_restarting()
        self.assertTrue(notify.restarting())
        notify.clear_restarting()
        self.assertFalse(notify.restarting())

    def test_stale_flag_ignored(self):
        """看门狗自己挂了、标记留在盘上 → 不能把通知通道永久堵死。"""
        self._flag_on(age=notify._RESTART_FLAG_TTL + 10)
        self.assertFalse(notify.restarting())

    def test_tick_defers_to_watchdog(self):
        w = notify._Watcher(self.qr.path)
        self._flag_on()
        self.qr.touch(b"png-2", at=w.last_mtime + 10)
        self.assertFalse(w.tick())
        self.assertEqual(self.sent, [])
        # 让位期间收下了 mtime：窗口期结束后不会把旧码补推出来
        notify.clear_restarting()
        self.assertFalse(w.tick())
        self.assertEqual(self.sent, [])

    def test_prime_defers_to_watchdog(self):
        self._flag_on()
        self.assertFalse(notify._Watcher(self.qr.path).prime())
        self.assertEqual(self.sent, [])


class SilenceAlertTest(unittest.TestCase):
    """静默告警：连续 N 小时零收发 + 探活正常 → 推「疑似冻结」。

    抓的是「冻而不掉」（2026-09-27 事故：协议层活着、消息同步停摆，
    二维码机制完全无感）。一轮静默只推一次，恢复活动后重置。
    """

    def setUp(self):
        self.qr = _TmpQR(self)
        for name, val in (("NOTIFY_PUSHPLUS_TOKEN", "tok"),
                          ("NOTIFY_QRCODE_PATH", self.qr.path),
                          ("NOTIFY_SILENCE_HOURS", 4)):
            p = mock.patch.object(notify, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.sent = []
        p = mock.patch.object(notify, "_post", _post_capture(self.sent))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(notify, "_online", lambda: True)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(notify, "_last_activity", 0.0)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(notify, "_silence_alerted", False)
        p.start()
        self.addCleanup(p.stop)

    def test_silent_and_alive_pushes_once(self):
        """静默满 4 小时 + 在线 → 推一条；同一轮静默内不重复推。"""
        w = notify._Watcher(self.qr.path)
        self.assertTrue(w.tick(now=5 * 3600))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("疑似冻结", self.sent[0]["title"])
        self.assertFalse(w.tick(now=5 * 3600 + 600))
        self.assertEqual(len(self.sent), 1)

    def test_recent_activity_no_push(self):
        """刚有活动不久 → 不推。"""
        w = notify._Watcher(self.qr.path)
        self.assertFalse(w.tick(now=2 * 3600))
        self.assertEqual(self.sent, [])

    def test_offline_does_not_push_silence(self):
        """探活失败 = 真掉线，二维码路径负责，静默告警不抢。"""
        with mock.patch.object(notify, "_online", lambda: False):
            w = notify._Watcher(self.qr.path)
            self.assertFalse(w.tick(now=9 * 3600))
        self.assertEqual(self.sent, [])

    def test_activity_resets_alert(self):
        """恢复活动后重置：下一次静默满时长可以再报。"""
        w = notify._Watcher(self.qr.path)
        self.assertTrue(w.tick(now=5 * 3600))
        notify.note_activity()                       # 活动了
        self.assertFalse(w.tick(now=5 * 3600 + 600))
        # 再静默满 4 小时，又能报
        p = mock.patch.object(notify, "_last_activity", 5 * 3600)
        p.start()
        self.addCleanup(p.stop)
        self.assertTrue(w.tick(now=10 * 3600))
        self.assertEqual(len(self.sent), 2)

    def test_zero_disables(self):
        with mock.patch.object(notify, "NOTIFY_SILENCE_HOURS", 0):
            w = notify._Watcher(self.qr.path)
            self.assertFalse(w.tick(now=9 * 3600))
        self.assertEqual(self.sent, [])


class DispatchHeartbeatTest(unittest.TestCase):
    """qq_bot 收到消息事件要打静默告警的心跳（收发共用这一个入口）。"""

    def setUp(self):
        from app import qq_bot
        self.qq_bot = qq_bot
        self.bot = qq_bot.QQBot()

    def test_message_event_notes_activity(self):
        # 心跳打点在判定之前；关掉接话免得走到 create_task（测试没有事件循环）
        with mock.patch.object(self.qq_bot.notify, "note_activity") as na, \
             mock.patch.object(self.qq_bot.interject, "enabled",
                               lambda: False):
            self.bot._dispatch(json.dumps({
                "post_type": "message", "message_type": "group",
                "group_id": 123, "user_id": 456, "self_id": 999,
                "sender": {"nickname": "张三"}, "message": "在吗"}))
        na.assert_called_once()

    def test_non_message_event_no_heartbeat(self):
        with mock.patch.object(self.qq_bot.notify, "note_activity") as na:
            self.bot._dispatch(json.dumps({
                "post_type": "meta_event", "meta_event_type": "heartbeat"}))
        na.assert_not_called()


if __name__ == "__main__":
    unittest.main()
