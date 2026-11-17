# -*- coding: utf-8 -*-
"""服务控制台：服务清单、状态颜色、API 鉴权与重启动作。

这些测试不启动真实进程；进程托管器用 FakeManager 顶替。
"""

import unittest
from pathlib import Path

from app.features.control.panel import create_app
from app.features.control.specs import default_specs, status_color, status_label


class FakeManager:
    specs = {"agent": object(), "qqbot": object(), "comfyui": object()}

    def __init__(self):
        self.calls = []

    def _check(self, sid):
        if sid not in self.specs:
            raise KeyError(sid)

    def status(self):
        return {
            "ok": True,
            "services": [
                {
                    "id": "agent",
                    "name": "Agent Web",
                    "group": "核心",
                    "status": "running",
                    "color": "green",
                    "label": "运行中",
                    "detail": "PID 123 · 5174",
                    "pid": 123,
                    "port": 5174,
                    "restarts": 0,
                },
                {
                    "id": "qqbot",
                    "name": "QQ 机器人",
                    "group": "QQ",
                    "status": "waiting",
                    "color": "yellow",
                    "label": "等待登录",
                    "detail": "协议端未登录",
                    "pid": 456,
                    "port": None,
                    "restarts": 1,
                },
                {
                    "id": "comfyui",
                    "name": "ComfyUI",
                    "group": "可选",
                    "status": "stopped",
                    "color": "red",
                    "label": "已停止",
                    "detail": "未启动",
                    "pid": None,
                    "port": 8188,
                    "restarts": 0,
                },
            ],
            "summary": {"running": 1, "yellow": 1, "stopped": 1, "total": 3},
        }

    def restart(self, sid):
        self._check(sid)
        self.calls.append((sid, "restart"))
        return {"ok": True, "status": "restarting"}

    def start(self, sid):
        self._check(sid)
        self.calls.append((sid, "start"))
        return {"ok": True, "status": "starting"}

    def stop(self, sid):
        self._check(sid)
        self.calls.append((sid, "stop"))
        return {"ok": True, "status": "stopping"}

    def restart_all(self):
        self.calls.append(("*", "restart"))
        return {"ok": True, "status": "restarting"}


class SpecsTest(unittest.TestCase):
    def test_default_specs_cover_the_nine_service_windows(self):
        ids = {spec.id for spec in default_specs()}
        self.assertEqual(
            {
                "agent",
                "friend",
                "friend_tunnel",
                "admin",
                "admin_tunnel",
                "snowluma",
                "qqbot",
                "watchdog",
                "comfyui",
            },
            ids,
        )

    def test_admin_tunnel_points_at_5176_and_uses_its_own_url_file(self):
        admin = next(s for s in default_specs() if s.id == "admin_tunnel")
        self.assertEqual("http://127.0.0.1:5176", admin.env["FRIEND_TUNNEL_TARGET"])
        self.assertEqual("admin_url.txt", admin.env["FRIEND_TUNNEL_URL_FILE"])

    def test_agent_command_starts_the_root_entry(self):
        agent = next(s for s in default_specs() if s.id == "agent")
        self.assertTrue(agent.command[-1].endswith("agent.py"))
        self.assertEqual(5174, agent.port)

    def test_watchdog_and_comfyui_stay_manual(self):
        specs = {s.id: s for s in default_specs()}
        self.assertTrue(specs["agent"].autostart)
        self.assertFalse(specs["watchdog"].autostart)
        self.assertFalse(specs["comfyui"].autostart)

    def test_launcher_is_minimal_and_does_not_use_path_commands(self):
        bat = (Path(__file__).resolve().parents[1] / "启动控制台.bat").read_text(
            encoding="utf-8")
        self.assertIn('start "" "%PYW%" -m app.features.control.panel', bat)
        self.assertNotIn("netstat", bat.lower())
        self.assertNotIn("findstr", bat.lower())
        self.assertNotIn("timeout", bat.lower())
        self.assertNotIn("goto wait", bat.lower())

    def test_status_colors_are_three_state(self):
        self.assertEqual("green", status_color("running"))
        for state in ("starting", "restarting", "waiting"):
            self.assertEqual("yellow", status_color(state))
        for state in ("stopped", "error"):
            self.assertEqual("red", status_color(state))

    def test_status_labels_are_human_readable(self):
        self.assertEqual("运行中", status_label("running"))
        self.assertEqual("重启中", status_label("restarting"))
        self.assertEqual("等待登录", status_label("waiting"))
        self.assertEqual("已停止", status_label("stopped"))


class PanelApiTest(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager()
        self.app = create_app(self.manager, token="test-token")
        self.client = self.app.test_client()

    def test_status_requires_token(self):
        r = self.client.get("/api/status")
        self.assertEqual(401, r.status_code)

    def test_status_returns_nine_cards_payload(self):
        r = self.client.get("/api/status", headers={"X-Control-Token": "test-token"})
        self.assertEqual(200, r.status_code)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(3, len(data["services"]))
        self.assertEqual("green", data["services"][0]["color"])

    def test_restart_calls_manager_once(self):
        r = self.client.post(
            "/api/services/agent/restart",
            headers={"X-Control-Token": "test-token"},
        )
        self.assertEqual(200, r.status_code)
        self.assertEqual([("agent", "restart")], self.manager.calls)
        self.assertEqual("restarting", r.get_json()["status"])

    def test_start_and_stop_routes(self):
        self.client.post(
            "/api/services/qqbot/start",
            headers={"X-Control-Token": "test-token"},
        )
        self.client.post(
            "/api/services/qqbot/stop",
            headers={"X-Control-Token": "test-token"},
        )
        self.assertEqual([("qqbot", "start"), ("qqbot", "stop")], self.manager.calls)

    def test_unknown_service_is_404(self):
        r = self.client.post(
            "/api/services/nope/restart",
            headers={"X-Control-Token": "test-token"},
        )
        self.assertEqual(404, r.status_code)

    def test_page_injects_token_and_is_square_dashboard(self):
        r = self.client.get("/")
        self.assertEqual(200, r.status_code)
        html = r.get_data(as_text=True)
        self.assertIn("test-token", html)
        self.assertIn("服务控制台", html)
        self.assertIn("grid-template-columns: repeat(3", html)


if __name__ == "__main__":
    unittest.main()
