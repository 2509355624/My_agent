# -*- coding: utf-8 -*-
"""私聊闸测试：settings.json 优先（管理页热改），缺省回落 .env 常量。

对应 app/qq_bot.py 的 _private_gate 与 app/main.py 的两个私聊管理路由。
关键语义（用户 2026-09-26 拍板）：
- settings 写了 private_whitelist 键 → 真白名单：名单外静默不回，空名单 = 谁都不行；
- 两个键都没写 → 老部署回落 .env（名单空 = 不限制），行为不变；
- 黑名单（.env）在更早的位置拦截，优先级永远最高。
隔离铁律：碰 agents/ 数据必须换整根 AGENTS_DIR 到临时目录。
"""

import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.main as main
import app.qq_bot as qq_bot


class PrivateGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        # settings 没写键时的回落值（等价于当前 .env：私聊开、名单空）
        for name, value in (
            ("QQ_PRIVATE_ENABLE", True),
            ("QQ_WHITELIST_USERS", []),
            ("QQ_BLACKLIST_USERS", []),
        ):
            p = mock.patch.object(qq_bot, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_settings(self, **kw):
        agents.save_settings("qq", kw)

    def _reply(self, user_id="1"):
        return qq_bot._should_reply({"user_id": user_id}, "private", user_id,
                                    "你好", False)

    # ── 回落路径：settings 没写键，行为与改造前一致 ──

    def test_no_settings_falls_back_to_env(self):
        ok, why = self._reply()
        self.assertTrue(ok)
        self.assertIn("私聊", why)

    def test_env_private_off_still_works(self):
        p = mock.patch.object(qq_bot, "QQ_PRIVATE_ENABLE", False)
        p.start()
        self.addCleanup(p.stop)
        ok, _ = self._reply()
        self.assertFalse(ok)

    def test_env_whitelist_fallback(self):
        p = mock.patch.object(qq_bot, "QQ_WHITELIST_USERS", ["7"])
        p.start()
        self.addCleanup(p.stop)
        ok, _ = self._reply(user_id="8")
        self.assertFalse(ok)
        ok, _ = self._reply(user_id="7")
        self.assertTrue(ok)

    # ── settings 优先：管理页写过的键说了算 ──

    def test_settings_enable_off_beats_env(self):
        self.write_settings(private_enable=False)
        ok, why = self._reply()
        self.assertFalse(ok)
        self.assertIn("管理员", why)

    def test_settings_whitelist_strict(self):
        self.write_settings(private_whitelist=["2509355624"])
        ok, why = self._reply(user_id="999")
        self.assertFalse(ok)
        self.assertIn("白名单", why)
        ok, _ = self._reply(user_id="2509355624")
        self.assertTrue(ok)

    def test_settings_empty_whitelist_denies_all(self):
        # 管理页语义：写过白名单键后，空名单 = 谁都私聊不了
        self.write_settings(private_whitelist=[])
        ok, why = self._reply()
        self.assertFalse(ok)
        self.assertIn("白名单为空", why)

    def test_settings_enable_on_with_empty_whitelist_denies(self):
        self.write_settings(private_enable=True, private_whitelist=[])
        ok, _ = self._reply()
        self.assertFalse(ok)

    def test_blacklist_beats_whitelist(self):
        self.write_settings(private_whitelist=["1"])
        p = mock.patch.object(qq_bot, "QQ_BLACKLIST_USERS", ["1"])
        p.start()
        self.addCleanup(p.stop)
        ok, _ = self._reply(user_id="1")
        self.assertFalse(ok)

    def test_no_text_still_skipped(self):
        # 闸放行了，但只发图/表情没有文字仍不打扰模型
        self.write_settings(private_whitelist=["1"])
        ok, _ = qq_bot._should_reply({"user_id": "1"}, "private", "1",
                                     "", False)
        self.assertFalse(ok)


class PrivateAdminApiTest(unittest.TestCase):
    """管理页的两个私聊路由：总开关 + 白名单增删。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        p = mock.patch.object(main, "ADMIN_ALLOW_REMOTE", True)
        p.start()
        self.addCleanup(p.stop)
        self.client = main.app.test_client()

    def url(self, path):
        return "/api/agent/qq/" + path

    def test_enable_writes_settings(self):
        r = self.client.put(self.url("private_enable"), json={"enabled": False})
        self.assertEqual(r.status_code, 200)
        s = agents.load_settings("qq")
        self.assertFalse(s["private_enable"])

    def test_enable_needs_bool(self):
        r = self.client.put(self.url("private_enable"), json={"enabled": "yes"})
        self.assertEqual(r.status_code, 400)

    def test_whitelist_add_and_remove(self):
        r = self.client.put(self.url("private_whitelist"),
                            json={"op": "add", "user_id": "2509355624"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["private_whitelist"], ["2509355624"])
        r = self.client.put(self.url("private_whitelist"),
                            json={"op": "add", "user_id": "42"})
        self.assertEqual(r.get_json()["private_whitelist"],
                         ["2509355624", "42"])   # 有序去重
        r = self.client.put(self.url("private_whitelist"),
                            json={"op": "remove", "user_id": "2509355624"})
        self.assertEqual(r.get_json()["private_whitelist"], ["42"])

    def test_whitelist_add_is_idempotent(self):
        for _ in range(2):
            r = self.client.put(self.url("private_whitelist"),
                                json={"op": "add", "user_id": "42"})
            self.assertEqual(r.get_json()["private_whitelist"], ["42"])

    def test_whitelist_rejects_non_numeric(self):
        r = self.client.put(self.url("private_whitelist"),
                            json={"op": "add", "user_id": "abc"})
        self.assertEqual(r.status_code, 400)

    def test_whitelist_rejects_unknown_op(self):
        r = self.client.put(self.url("private_whitelist"),
                            json={"op": "clear", "user_id": "42"})
        self.assertEqual(r.status_code, 400)

    def test_sessions_payload_carries_private_state(self):
        agents.save_settings("qq", {"private_enable": False,
                                    "private_whitelist": ["42"]})
        r = self.client.get(self.url("sessions"))
        d = r.get_json()
        self.assertFalse(d["private_enable"])
        self.assertEqual(d["private_whitelist"], ["42"])


if __name__ == "__main__":
    unittest.main()
