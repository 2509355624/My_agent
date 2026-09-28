"""comfy_status：ComfyUI 状态探测与状态栏文案。

测试不走真网络：patch `comfy_status._session`，让它返回脚本化的响应。
缓存相关用例直接操作 `_CACHE` / `reset_cache()`。
"""

import unittest
from unittest import mock

from app import comfy_status


def _resp(payload, status=200):
    m = mock.Mock()
    m.status_code = status
    m.json.return_value = payload
    m.raise_for_status = mock.Mock()
    if status >= 400:
        m.raise_for_status.side_effect = RuntimeError("http %s" % status)
    return m


class SnapshotProbeTest(unittest.TestCase):
    def setUp(self):
        comfy_status.reset_cache()

    def tearDown(self):
        comfy_status.reset_cache()

    def test_online_with_queue_counts(self):
        with mock.patch.object(
                comfy_status, "_session") as ses:
            ses.get.side_effect = [
                _resp({}),                      # /system_stats
                _resp({"queue_running": [1], "queue_pending": [2, 3]}),
            ]
            s = comfy_status.snapshot()
        self.assertTrue(s["online"])
        self.assertEqual(s["running"], 1)
        self.assertEqual(s["pending"], 2)

    def test_offline_when_system_stats_fails(self):
        with mock.patch.object(
                comfy_status, "_session") as ses:
            ses.get.side_effect = ConnectionError("refused")
            s = comfy_status.snapshot()
        self.assertFalse(s["online"])
        self.assertEqual((s["running"], s["pending"]), (0, 0))

    def test_queue_failure_still_online(self):
        # /system_stats 通 = 在线；/queue 炸了只影响计数
        with mock.patch.object(
                comfy_status, "_session") as ses:
            ok = _resp({})
            bad = mock.Mock()
            bad.json.side_effect = ValueError("bad json")
            ses.get.side_effect = [ok, bad]
            s = comfy_status.snapshot()
        self.assertTrue(s["online"])
        self.assertEqual((s["running"], s["pending"]), (0, 0))

    def test_ttl_cache_skips_second_probe(self):
        with mock.patch.object(
                comfy_status, "_session") as ses:
            ses.get.side_effect = [_resp({}), _resp({"queue_running": []})]
            comfy_status.snapshot()
            comfy_status.snapshot()          # TTL 内第二次：不再探测
            self.assertEqual(ses.get.call_count, 2)

    def test_force_bypasses_cache(self):
        with mock.patch.object(
                comfy_status, "_session") as ses:
            ses.get.side_effect = [_resp({}), _resp({}),
                                   _resp({}), _resp({})]
            comfy_status.snapshot()
            comfy_status.snapshot(force=True)
            self.assertEqual(ses.get.call_count, 4)


class StatusLineTest(unittest.TestCase):
    def setUp(self):
        comfy_status.reset_cache()

    def tearDown(self):
        comfy_status.reset_cache()

    def test_online_idle(self):
        with mock.patch.object(
                comfy_status, "snapshot",
                return_value={"online": True, "running": 0, "pending": 0}):
            line = comfy_status.status_line()
        self.assertIn("online", line)
        self.assertIn("队列空闲", line)

    def test_online_busy(self):
        with mock.patch.object(
                comfy_status, "snapshot",
                return_value={"online": True, "running": 1, "pending": 2}):
            line = comfy_status.status_line()
        self.assertIn("正在画 1 张", line)
        self.assertIn("排队 2 张", line)

    def test_offline_forbids_promising_images(self):
        with mock.patch.object(
                comfy_status, "snapshot",
                return_value={"online": False, "running": 0, "pending": 0}):
            line = comfy_status.status_line()
        self.assertIn("OFFLINE", line)
        self.assertIn("不要答应画图请求", line)
        self.assertIn("127.0.0.1:8188", line)


class StatusBarWiringTest(unittest.TestCase):
    """接线测试：build_status_bar 必须真的把状态行拼进去（防重构悄悄断掉）。"""

    def test_status_bar_contains_comfyui_line(self):
        from app import agent_prompt
        with mock.patch.object(
                comfy_status, "snapshot",
                return_value={"online": True, "running": 1, "pending": 2}):
            bar = agent_prompt.build_status_bar()
        self.assertIn("<status_bar>", bar)
        self.assertIn("comfyui: online；正在画 1 张、排队 2 张", bar)
        # 行必须在标签内部，不能漏到 </status_bar> 之外
        self.assertLess(bar.index("comfyui:"), bar.index("</status_bar>"))


if __name__ == "__main__":
    unittest.main()
