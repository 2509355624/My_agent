"""comfy_status：ComfyUI 状态探测 + 状态栏的 NAI 队列行。

测试不走真网络：patch `comfy_status._session`，让它返回脚本化的响应。
缓存相关用例直接操作 `_CACHE` / `reset_cache()`。

2026-09-29：状态栏不再报 ComfyUI 的死活（只留 NAI 行），所以这里没有
`status_line()` 的用例了——`snapshot()` 现在只服务网页状态后台。
"""

import unittest
from unittest import mock

from app import comfy_status
from app import image_jobs


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


class NaiLineTest(unittest.TestCase):
    """状态栏那一行：只报 agent 侧自己的 NAI 队列，不碰 ComfyUI。"""

    def test_nai_busy(self):
        with mock.patch.object(image_jobs, "nai_depth", return_value=(1, 2)):
            line = comfy_status.nai_line()
        self.assertIn("nai: 正在画 1 张、排队 2 张", line)

    def test_nai_idle(self):
        with mock.patch.object(image_jobs, "nai_depth", return_value=(0, 0)):
            line = comfy_status.nai_line()
        self.assertIn("nai: 空闲", line)

    def test_never_probes_comfyui(self):
        # 关键回归：这一行现在是状态栏唯一的动态项，绝不能顺带触发 ComfyUI
        # 探测（每轮一次 = 两次 HTTP）。探测只留给网页状态后台。
        with mock.patch.object(image_jobs, "nai_depth", return_value=(0, 0)), \
                mock.patch.object(comfy_status, "_session") as ses:
            comfy_status.nai_line()
        self.assertFalse(ses.get.called)


class NoComfyuiLeakTest(unittest.TestCase):
    """`status_line()` 已经删掉——它曾经把 ComfyUI 的死活直接喂给模型。"""

    def test_status_line_is_gone(self):
        self.assertFalse(hasattr(comfy_status, "status_line"))


class StatusBarWiringTest(unittest.TestCase):
    """接线测试：build_status_bar 必须真的拼进 NAI 行、且不泄露 ComfyUI 状态。"""

    def test_status_bar_has_nai_but_no_comfyui(self):
        from app import agent_prompt
        with mock.patch.object(image_jobs, "nai_depth", return_value=(0, 0)):
            bar = agent_prompt.build_status_bar()
        self.assertIn("<status_bar>", bar)
        self.assertIn("nai: 空闲", bar)
        # 行必须在标签内部，不能漏到 </status_bar> 之外
        self.assertLess(bar.index("nai:"), bar.index("</status_bar>"))
        # 2026-09-29 用户要求：模型不需要、也不该看到本机 ComfyUI 的状态
        self.assertNotIn("comfyui", bar)
        self.assertNotIn("OFFLINE", bar)


if __name__ == "__main__":
    unittest.main()
