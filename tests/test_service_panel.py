"""服务总控面板测试：判据、日志 tail、结构、异常不炸整页。全 mock，不碰真服务。"""

import os
import tempfile
from unittest import TestCase, mock

from app import service_panel as sp


class TailTest(TestCase):
    """tail()：只读尾部、读不到返回 None、空文件返回 []。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _write(self, text):
        p = os.path.join(self.dir.name, "t.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_fewer_lines_than_limit(self):
        p = self._write("a\nb\nc\n")
        self.assertEqual(sp.tail(p, lines=10), ["a", "b", "c"])

    def test_takes_last_n(self):
        p = self._write("\n".join(str(i) for i in range(100)) + "\n")
        got = sp.tail(p, lines=5)
        self.assertEqual(got, ["95", "96", "97", "98", "99"])

    def test_empty_file(self):
        p = self._write("")
        self.assertEqual(sp.tail(p), [])

    def test_missing_file_returns_none(self):
        self.assertIsNone(sp.tail(os.path.join(self.dir.name, "nope.log")))

    def test_big_file_truncated_to_tail(self):
        """超过 max_bytes 时只读尾部——5MB 日志不能被整篇拉进轮询页面。"""
        p = self._write("x" * 200 + "\nEND\n")
        got = sp.tail(p, lines=5, max_bytes=64)
        self.assertEqual(got[-1], "END")


class TcpAliveTest(TestCase):

    def test_connect_ok(self):
        with mock.patch.object(sp.socket, "socket") as m:
            self.assertTrue(sp._tcp_alive(8188))

    def test_connect_refused_is_false(self):
        with mock.patch.object(sp.socket, "socket",
                               side_effect=OSError("refused")):
            self.assertFalse(sp._tcp_alive(8188))


class WatchdogAliveTest(TestCase):
    """判据同 bat：WATCHDOG 窗口的 cmd 在不在。非 Windows 返回 None（未知）。

    mock 的 stdout 用 bytes：实现按字节匹配（中文 Windows 上 tasklist 输出
    GBK，text=True 的 UTF-8 解码会让 reader 线程炸、stdout 缺斤少两）。
    """

    def test_window_present(self):
        with mock.patch.object(sp.subprocess, "run") as m:
            m.return_value = mock.Mock(stdout=b"cmd.exe  1234 Console\n")
            with mock.patch.object(sp.sys, "platform", "win32"):
                self.assertTrue(sp._watchdog_alive())

    def test_window_absent(self):
        with mock.patch.object(sp.subprocess, "run") as m:
            m.return_value = mock.Mock(stdout=b"No tasks are currently running.")
            with mock.patch.object(sp.sys, "platform", "win32"):
                self.assertFalse(sp._watchdog_alive())

    def test_non_windows_is_unknown(self):
        with mock.patch.object(sp.sys, "platform", "linux"):
            self.assertIsNone(sp._watchdog_alive())


class EntryTest(TestCase):
    """_comfy_entry / _bot_entry 返回 (online, detail) 元组——add() 按元组解包，
    曾写成返回 dict，迭代 dict 拿到键名，online 变成字符串 "online"。"""

    def test_comfy_offline(self):
        with mock.patch("app.comfy_status.snapshot",
                        return_value={"online": False, "running": 0,
                                      "pending": 0}):
            online, detail = sp._comfy_entry()
        self.assertFalse(online)
        self.assertIn("8188", detail)

    def test_comfy_online_vram_probe_failure_still_reports(self):
        """显存探测失败只丢那截信息，不能让整项变未知。"""
        with mock.patch("app.comfy_status.snapshot",
                        return_value={"online": True, "running": 1,
                                      "pending": 2}):
            with mock.patch("requests.get", side_effect=OSError("boom")):
                online, detail = sp._comfy_entry()
        self.assertTrue(online)
        self.assertIn("running 1 / pending 2", detail)

    def test_comfy_snapshot_explodes_is_unknown(self):
        with mock.patch("app.comfy_status.snapshot",
                        side_effect=RuntimeError("boom")):
            online, detail = sp._comfy_entry()
        self.assertIsNone(online)
        self.assertIn("探测失败", detail)

    def test_bot_entry_with_stale_snapshot(self):
        with mock.patch.object(sp, "_tcp_alive", return_value=True):
            with mock.patch("app.qq_status.read",
                            return_value=({"ts": 0}, True)):
                online, detail = sp._bot_entry()
        self.assertTrue(online)
        self.assertIn("停更", detail)


class LlamaPortTest(TestCase):

    def test_parsed_from_base_url(self):
        with mock.patch.object(sp, "LLAMA_BASE_URL",
                               "http://127.0.0.1:9999/v1"):
            self.assertEqual(sp._llama_port(), 9999)

    def test_fallback_when_unparseable(self):
        with mock.patch.object(sp, "LLAMA_BASE_URL", "::::"):
            self.assertEqual(sp._llama_port(), 8081)


class SnapshotTest(TestCase):
    """整页数据：6 个服务、结构完整、单项炸不掉整页。"""

    def test_all_offline_does_not_explode(self):
        with mock.patch.object(sp, "_tcp_alive", return_value=False), \
             mock.patch.object(sp, "_watchdog_alive", return_value=False), \
             mock.patch("app.comfy_status.snapshot",
                        return_value={"online": False, "running": 0,
                                      "pending": 0}):
            d = sp.snapshot()
        ids = [s["id"] for s in d["services"]]
        self.assertEqual(ids, ["web", "bot", "proto", "watchdog",
                               "comfy", "llama"])
        self.assertTrue(all(s["online"] is False or s["id"] == "web"
                            for s in d["services"]))
        self.assertIn("ts", d)

    def test_one_probe_explodes_others_survive(self):
        def boom():
            raise RuntimeError("nope")
        with mock.patch.object(sp, "_comfy_entry", side_effect=boom), \
             mock.patch.object(sp, "_tcp_alive", return_value=True), \
             mock.patch.object(sp, "_watchdog_alive", return_value=True):
            d = sp.snapshot()
        comfy = next(s for s in d["services"] if s["id"] == "comfy")
        self.assertIsNone(comfy["online"])
        self.assertIn("探测异常", comfy["detail"])
        self.assertTrue(any(s["online"] is True for s in d["services"]))

    def test_log_block_missing_file(self):
        b = sp._log_block(os.path.join(tempfile.gettempdir(),
                                       "definitely_not_here.log"))
        self.assertFalse(b["ok"])
        self.assertEqual(b["lines"], [])

    def test_log_block_no_path(self):
        b = sp._log_block("")
        self.assertFalse(b["ok"])
