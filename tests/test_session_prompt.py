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
        aid = qq_bot._session_prompt_agent(session_key) or qq_bot.QQ_AGENT_ID
        hist = qq_bot.load_history(aid, session_key)
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

    # ── 借用 agent：整份换成那个 agent 的完整系统提示词 ──

    def _make_agent(self, aid, persona):
        d = os.path.join(self.root, aid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            f.write('{"name": "借用测试"}')
        with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
            f.write(persona)

    def test_borrowed_agent_replaces_base(self):
        self._make_agent("main", "通用助手测试人设XYZMARK")
        agents.save_settings("qq", {"session_prompt_agents":
                                    {"group_1": "main"}})
        sys_prompt = self._system_of("group_1")
        self.assertIn("通用助手测试人设XYZMARK", sys_prompt)
        self.assertNotIn("会话专属人设", sys_prompt)

    def test_borrow_plus_extra_coexist(self):
        self._make_agent("main", "通用助手测试人设XYZMARK")
        agents.save_settings("qq", {
            "session_prompt_agents": {"group_1": "main"},
            "session_prompts": {"group_1": "附加规则ABC"}})
        sys_prompt = self._system_of("group_1")
        self.assertIn("通用助手测试人设XYZMARK", sys_prompt)
        self.assertIn("附加规则ABC", sys_prompt)

    def test_borrow_self_or_missing_falls_back(self):
        agents.save_settings("qq", {"session_prompt_agents":
                                    {"group_1": "qq", "group_2": "nope"}})
        self.assertNotIn("会话专属人设", self._system_of("group_1"))
        self.assertNotIn("会话专属人设", self._system_of("group_2"))

    def test_borrow_only_affects_its_session(self):
        self._make_agent("main", "通用助手测试人设XYZMARK")
        agents.save_settings("qq", {"session_prompt_agents":
                                    {"group_1": "main"}})
        self.assertNotIn("XYZMARK", self._system_of("group_2"))

    def test_borrow_history_lands_in_borrowed_agent_dir(self):
        self._make_agent("main", "通用助手测试人设XYZMARK")
        agents.save_settings("qq", {"session_prompt_agents":
                                    {"group_1": "main"}})
        self._system_of("group_1")
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "main", "sessions", "group_1.jsonl")))
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "qq", "sessions", "group_1.jsonl")))

    def test_sync_head_keeps_extra_and_updates(self):
        # 头里带附加词时，同步不许把它洗掉（sync_session_system 的老毛病）
        agents.save_settings("qq", {"session_prompts":
                                    {"group_1": "附加规则ABC"}})
        self._system_of("group_1")
        self.assertFalse(qq_bot._sync_session_head("group_1", "qq"))
        agents.save_settings("qq", {"session_prompts":
                                    {"group_1": "换成新规则DEF"}})
        self.assertTrue(qq_bot._sync_session_head("group_1", "qq"))
        from app.memory import peek_system
        head = peek_system("qq", "group_1")
        self.assertIn("新规则DEF", head)
        self.assertIn("[[TOOL:", head)

    def test_sync_head_logs_the_invalidation(self):
        """换头必须留痕。

        换头是**静默**发生的（人设改了、长期记忆写了、表情包清单变了都会
        触发），而它一发生这条会话的整段前缀缓存就作废、下一轮按全价重读。
        不留日志就没法回答「这条会话今天为什么突然全价」——2026-10-03 就是
        因为没这行，只能靠「落盘头 vs 现算头」逐条比对才发现 31 条全漂移。
        """
        self._system_of("group_1")
        agents.save_settings("qq", {"session_prompts":
                                    {"group_1": "换一批规则XYZ"}})
        with mock.patch.object(qq_bot.log, "info") as spy:
            self.assertTrue(qq_bot._sync_session_head("group_1", "qq"))
        self.assertTrue(
            any(c.args and "[head]" in str(c.args[0])
                for c in spy.call_args_list),
            "换头没打 [head] 日志：%r" % spy.call_args_list)

    def test_sync_head_silent_when_unchanged(self):
        """一字不差时不许打日志、更不许重写——那正是保住前缀缓存的地方。"""
        self._system_of("group_1")
        with mock.patch.object(qq_bot.log, "info") as spy:
            self.assertFalse(qq_bot._sync_session_head("group_1", "qq"))
        self.assertFalse(
            [c for c in spy.call_args_list
             if c.args and "[head]" in str(c.args[0])])


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


class SessionPromptBulkApiTest(unittest.TestCase):
    """批量应用会话提示词（管理页勾选多个群/私聊，一键应用同一个人设）。

    2026-10-03 用户提的需求：「可以勾选账号，或者群聊，然后一键应用我指定的
    人设，先单个搞很累的」。语义当场拍板：**只写填了的字段，没填的一律不动**
    —— 不是「留空 = 清除」（那会顺手清掉这些会话别处设的附加词/借用），
    想清除走 clear=true 或单条接口。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        # 造一个可借用的 agent（list_agents 是实时扫目录的）
        d = os.path.join(self.root, "main")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        p = mock.patch.object(main, "ADMIN_ALLOW_REMOTE", True)
        p.start()
        self.addCleanup(p.stop)
        self.client = main.app.test_client()

    def url(self, path):
        return "/api/agent/qq/" + path

    def bulk(self, **body):
        return self.client.put(self.url("session_prompt_bulk"), json=body)

    # ─── 正常路径 ────────────────────────────────────

    def test_applies_one_persona_to_many_sessions(self):
        r = self.bulk(keys=["group_1", "group_2", "private_42"],
                      system="你是只会说喵的猫娘。")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["applied"], 3)
        s = agents.load_settings("qq")["session_system_prompts"]
        self.assertEqual(sorted(s), ["group_1", "group_2", "private_42"])
        self.assertEqual(s["group_2"], "你是只会说喵的猫娘。")

    def test_untouched_fields_stay(self):
        """只填 system → 已有的附加词/借用原样不动（用户拍板的语义）。"""
        agents.save_settings("qq", {
            "session_prompts": {"group_1": "已有的附加词"},
            "session_prompt_agents": {"group_1": "main"},
            "session_system_prompts": {"group_1": "旧人设"}})
        r = self.bulk(keys=["group_1"], system="新人设")
        self.assertEqual(r.status_code, 200)
        s = agents.load_settings("qq")
        self.assertEqual(s["session_system_prompts"]["group_1"], "新人设")
        self.assertEqual(s["session_prompts"]["group_1"], "已有的附加词")
        self.assertEqual(s["session_prompt_agents"]["group_1"], "main")

    def test_three_fields_can_be_set_together(self):
        r = self.bulk(keys=["group_1"], system="人设X", text="附加Y",
                      agent="main")
        self.assertEqual(r.status_code, 200)
        s = agents.load_settings("qq")
        self.assertEqual(s["session_system_prompts"]["group_1"], "人设X")
        self.assertEqual(s["session_prompts"]["group_1"], "附加Y")
        self.assertEqual(s["session_prompt_agents"]["group_1"], "main")

    def test_clear_removes_all_three_and_drops_empty_keys(self):
        agents.save_settings("qq", {
            "session_prompts": {"group_1": "a", "group_2": "b"},
            "session_prompt_agents": {"group_1": "main"},
            "session_system_prompts": {"group_1": "c"}})
        r = self.bulk(keys=["group_1", "group_2"], clear=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["cleared"], 2)
        s = agents.load_settings("qq")
        # 三项都空了 → 整个键删掉，settings.json 别留空壳
        self.assertNotIn("session_system_prompts", s)
        self.assertNotIn("session_prompt_agents", s)
        self.assertNotIn("session_prompts", s)

    def test_clear_leaves_other_sessions_alone(self):
        agents.save_settings("qq", {"session_prompts": {"group_1": "a",
                                                       "group_9": "留着"}})
        self.bulk(keys=["group_1"], clear=True)
        s = agents.load_settings("qq")["session_prompts"]
        self.assertEqual(s, {"group_9": "留着"})

    # ─── 入参校验 ────────────────────────────────────

    def test_illegal_keys_are_skipped_not_fatal(self):
        r = self.bulk(keys=["group_1", "../../etc/passwd", "a/b", ""],
                      system="X")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["keys"], ["group_1"])
        self.assertEqual(len(d["skipped"]), 3)
        self.assertEqual(list(agents.load_settings("qq")
                              ["session_system_prompts"]), ["group_1"])

    def test_all_illegal_keys_is_400(self):
        self.assertEqual(self.bulk(keys=["../x", "a/b"], system="X")
                         .status_code, 400)

    def test_duplicate_keys_are_deduped(self):
        r = self.bulk(keys=["group_1", "group_1"], system="X")
        self.assertEqual(r.get_json()["keys"], ["group_1"])
        self.assertEqual(r.get_json()["applied"], 1)

    def test_needs_keys(self):
        self.assertEqual(self.bulk(system="X").status_code, 400)
        self.assertEqual(self.bulk(keys=[], system="X").status_code, 400)
        self.assertEqual(self.bulk(keys="group_1", system="X").status_code, 400)

    def test_too_many_keys_is_400(self):
        r = self.bulk(keys=["group_%d" % i for i in range(501)], system="X")
        self.assertEqual(r.status_code, 400)

    def test_needs_at_least_one_field(self):
        self.assertEqual(self.bulk(keys=["group_1"]).status_code, 400)
        # 只有空白也不算填了
        self.assertEqual(self.bulk(keys=["group_1"], system="  ",
                                   text="").status_code, 400)

    def test_non_string_field_is_400(self):
        self.assertEqual(self.bulk(keys=["group_1"], system=123).status_code,
                         400)
        self.assertEqual(self.bulk(keys=["group_1"], text=["x"]).status_code,
                         400)

    def test_borrow_must_exist(self):
        r = self.bulk(keys=["group_1"], agent="查无此人")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("session_prompt_agents", agents.load_settings("qq"))

    def test_borrow_self_is_rejected_not_silent(self):
        """借自己 = 等于不借。必须报错，不能 200 却什么都不写（那最难查）。"""
        r = self.bulk(keys=["group_1"], agent="qq")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("session_prompt_agents", agents.load_settings("qq"))

    # ── 借用 agent 接口 ──

    def _mk_agent(self, aid):
        d = os.path.join(self.root, aid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            f.write('{"name": "借用测试"}')

    def test_borrow_save_and_payload(self):
        self._mk_agent("main")
        r = self.client.put(self.url("session_prompt/group_1"),
                            json={"text": "", "agent": "main"})
        self.assertEqual(r.status_code, 200)
        s = agents.load_settings("qq")
        self.assertEqual(s["session_prompt_agents"]["group_1"], "main")
        d = self.client.get(self.url("sessions")).get_json()
        self.assertEqual(d["session_prompt_agents"]["group_1"], "main")

    def test_borrow_invalid_rejected(self):
        r = self.client.put(self.url("session_prompt/group_1"),
                            json={"text": "", "agent": "nope"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("session_prompt_agents",
                         agents.load_settings("qq"))

    def test_borrow_clear_back_to_default(self):
        self._mk_agent("main")
        self.client.put(self.url("session_prompt/group_1"),
                        json={"text": "", "agent": "main"})
        r = self.client.put(self.url("session_prompt/group_1"),
                            json={"text": "", "agent": ""})
        self.assertEqual(r.status_code, 200)
        s = agents.load_settings("qq")
        self.assertNotIn("session_prompt_agents", s)

    def test_text_only_save_clears_borrow(self):
        self._mk_agent("main")
        self.client.put(self.url("session_prompt/group_1"),
                        json={"text": "", "agent": "main"})
        self.client.put(self.url("session_prompt/group_1"),
                        json={"text": "普通自定义"})
        s = agents.load_settings("qq")
        self.assertNotIn("session_prompt_agents", s)
        self.assertEqual(s["session_prompts"]["group_1"], "普通自定义")


if __name__ == "__main__":
    unittest.main()
