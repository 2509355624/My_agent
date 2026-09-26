# -*- coding: utf-8 -*-
"""会话级自定义提示词测试：settings.json 的 session_prompts。

对应 app/qq_bot.py 的 _ensure_system_prompt / _session_prompt_extra
与 app/main.py 的 PUT /session_prompt/<key> 路由。
语义（用户 2026-09-26 拍板）：
- 追加式：稳定层（人设+工具规则）保留，自定义词拼在后面一个独立段落；
- 没配 = 原样，什么都不加；
- 热生效：qq_bot 每轮重建首条 system，管理页保存即生效。
隔离铁律：碰 agents/ 数据必须换整根 AGENTS_DIR 到临时目录。
"""

import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.main as main
import app.qq_bot as qq_bot


class SessionPromptGateTest(unittest.TestCase):
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

    def _system_of(self, session_key):
        qq_bot._ensure_system_prompt(session_key)
        hist = qq_bot.load_history(qq_bot.QQ_AGENT_ID, session_key)
        return hist[0]["content"] if hist else ""

    def test_no_config_keeps_plain_stable(self):
        sys_prompt = self._system_of("group_1")
        self.assertNotIn("会话专属人设", sys_prompt)

    def test_extra_appended_as_own_section(self):
        agents.save_settings("qq", {"session_prompts": {
            "group_1": "在这个群里你说话更毒舌"}})
        sys_prompt = self._system_of("group_1")
        self.assertIn("会话专属人设", sys_prompt)
        self.assertIn("更毒舌", sys_prompt)
        # 稳定层还在（工具协议不能丢）
        self.assertIn("[[TOOL:", sys_prompt)

    def test_other_sessions_unaffected(self):
        agents.save_settings("qq", {"session_prompts": {
            "group_1": "毒舌版"}})
        self.assertNotIn("会话专属人设", self._system_of("group_2"))

    def test_clearing_text_back_to_default(self):
        agents.save_settings("qq", {"session_prompts": {"group_1": "毒舌版"}})
        self.assertIn("会话专属人设", self._system_of("group_1"))
        agents.save_settings("qq", {"session_prompts": {}})
        self.assertNotIn("会话专属人设", self._system_of("group_1"))

    def test_blank_string_ignored(self):
        agents.save_settings("qq", {"session_prompts": {"group_1": "   "}})
        self.assertNotIn("会话专属人设", self._system_of("group_1"))


class SessionPromptApiTest(unittest.TestCase):
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

    def test_save_and_payload(self):
        r = self.client.put(self.url("session_prompt/group_1"),
                            json={"text": "毒舌版"})
        self.assertEqual(r.status_code, 200)
        s = agents.load_settings("qq")
        self.assertEqual(s["session_prompts"]["group_1"], "毒舌版")
        d = self.client.get(self.url("sessions")).get_json()
        self.assertEqual(d["session_prompts"]["group_1"], "毒舌版")

    def test_empty_text_removes_entry(self):
        agents.save_settings("qq", {"session_prompts": {"group_1": "x"}})
        r = self.client.put(self.url("session_prompt/group_1"),
                            json={"text": "  "})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(agents.load_settings("qq").get("session_prompts"), {})

    def test_needs_text_field(self):
        r = self.client.put(self.url("session_prompt/group_1"), json={})
        self.assertEqual(r.status_code, 400)
        r = self.client.put(self.url("session_prompt/group_1"),
                            json={"text": 123})
        self.assertEqual(r.status_code, 400)

    def test_multiple_sessions_coexist(self):
        for key, text in (("group_1", "A 群人设"), ("private_42", "私聊人设")):
            self.client.put(self.url("session_prompt/" + key),
                            json={"text": text})
        s = agents.load_settings("qq")["session_prompts"]
        self.assertEqual(s, {"group_1": "A 群人设", "private_42": "私聊人设"})


if __name__ == "__main__":
    unittest.main()
