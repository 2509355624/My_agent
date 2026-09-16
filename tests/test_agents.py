# -*- coding: utf-8 -*-
"""多 agent 隔离测试（app/agents.py）。

关注四点：
1. 路径安全——agent_id 会被拼进文件路径，必须挡住 ../ 之类的穿越；
2. 白名单语义——None 表示不限制，列表表示白名单，空列表表示什么都不给；
3. 热加载——改了 agent.json / prompt.md 后按 mtime 自动重读，不用重启服务；
4. 容错——配置写坏、文件缺失、目录不存在都不能让服务崩。
"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import app.agents as agents


def _bump(path):
    """把文件 mtime 拨到未来，绕开文件系统 mtime 精度导致的「看起来没变」。"""
    later = time.time() + 10
    os.utime(path, (later, later))


class AgentsTestBase(unittest.TestCase):
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

    def make_agent(self, aid, cfg=None, prompt=None):
        d = os.path.join(self.root, aid)
        os.makedirs(d, exist_ok=True)
        if cfg is not None:
            with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)
        if prompt is not None:
            with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
                f.write(prompt)
        return d


class SafeAgentIdTest(AgentsTestBase):
    def test_accepts_normal_ids(self):
        for ok in ("main", "writing", "agent_1", "A-b", "x" * 64):
            self.assertEqual(agents.safe_agent_id(ok), ok)

    def test_rejects_traversal_and_separators(self):
        for bad in ("../evil", "..", "a/b", "a\\b", "/abs", "", "   ",
                    "a b", ".hidden", "-lead", "x" * 65):
            self.assertIsNone(agents.safe_agent_id(bad), bad)

    def test_rejects_non_string(self):
        for bad in (None, 123, [], {}):
            self.assertIsNone(agents.safe_agent_id(bad))

    def test_session_file_of_illegal_id_falls_back_to_default(self):
        path = agents.session_file("../evil")
        self.assertEqual(path, os.path.join(self.root, "main", "session.jsonl"))

    def test_session_file_of_valid_id(self):
        self.assertEqual(agents.session_file("writing"),
                         os.path.join(self.root, "writing", "session.jsonl"))


class ResolveTest(AgentsTestBase):
    def test_returns_existing_agent(self):
        self.make_agent("writing")
        self.assertEqual(agents.resolve("writing"), "writing")

    def test_falls_back_when_missing_or_illegal(self):
        for bad in (None, "", "nope", "../evil", "a/b"):
            self.assertEqual(agents.resolve(bad), "main")


class AgentConfigTest(AgentsTestBase):
    def test_reads_config(self):
        self.make_agent("writing", {"name": "写作助手", "tools": ["read_file"],
                                    "skills": ["writing"]})
        cfg = agents.agent_config("writing")
        self.assertEqual(cfg["name"], "写作助手")
        self.assertEqual(cfg["tools"], ["read_file"])
        self.assertEqual(cfg["skills"], ["writing"])

    def test_missing_config_returns_defaults(self):
        self.make_agent("plain")
        cfg = agents.agent_config("plain")
        self.assertIsNone(cfg["tools"])
        self.assertIsNone(cfg["skills"])
        self.assertEqual(cfg["prompt_file"], "prompt.md")

    def test_broken_json_falls_back_to_defaults(self):
        d = self.make_agent("broken")
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            f.write("{这不是合法 json")
        self.assertIsNone(agents.agent_config("broken")["tools"])

    def test_non_list_whitelist_treated_as_unlimited(self):
        self.make_agent("weird", {"tools": "read_file"})
        self.assertIsNone(agents.agent_config("weird")["tools"])

    def test_whitelist_dedup_strip_and_drop_non_strings(self):
        self.make_agent("d", {"tools": ["read_file", " read_file ", "", 123]})
        self.assertEqual(agents.agent_config("d")["tools"], ["read_file"])

    def test_unknown_fields_ignored(self):
        self.make_agent("x", {"tools": None, "whatever": 1})
        self.assertNotIn("whatever", agents.agent_config("x"))

    def test_hot_reload_after_config_change(self):
        d = self.make_agent("hot", {"tools": ["read_file"]})
        self.assertEqual(agents.agent_config("hot")["tools"], ["read_file"])
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            json.dump({"tools": ["write_file"]}, f)
        _bump(os.path.join(d, "agent.json"))
        self.assertEqual(agents.agent_config("hot")["tools"], ["write_file"])


class PersonaTest(AgentsTestBase):
    def test_reads_prompt_file(self):
        self.make_agent("w", prompt="你是写作助手。")
        self.assertEqual(agents.persona_text("w"), "你是写作助手。")

    def test_missing_prompt_returns_empty(self):
        self.make_agent("w")
        self.assertEqual(agents.persona_text("w"), "")

    def test_inline_prompt_used_when_file_missing(self):
        self.make_agent("w", {"prompt": "内联人设"})
        self.assertEqual(agents.persona_text("w"), "内联人设")

    def test_prompt_file_cannot_escape_agent_dir(self):
        d = self.make_agent("w", {"prompt_file": "../outside.md"})
        with open(os.path.join(self.root, "outside.md"), "w", encoding="utf-8") as f:
            f.write("不该被读到")
        self.assertEqual(agents.persona_text("w"), "")
        self.assertTrue(os.path.exists(os.path.join(self.root, "outside.md")))
        self.assertTrue(os.path.isdir(d))

    def test_hot_reload_after_prompt_change(self):
        d = self.make_agent("w", prompt="第一版")
        self.assertEqual(agents.persona_text("w"), "第一版")
        with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
            f.write("第二版")
        _bump(os.path.join(d, "prompt.md"))
        self.assertEqual(agents.persona_text("w"), "第二版")


class WhitelistTest(AgentsTestBase):
    def test_none_means_unlimited(self):
        self.make_agent("any")
        self.assertTrue(agents.allows_tool("any", "generate_image"))
        self.assertTrue(agents.allows_skill("any", "anything"))

    def test_whitelist_limits(self):
        self.make_agent("w", {"tools": ["read_file"], "skills": ["writing"]})
        self.assertTrue(agents.allows_tool("w", "read_file"))
        self.assertFalse(agents.allows_tool("w", "generate_image"))
        self.assertTrue(agents.allows_skill("w", "writing"))
        self.assertFalse(agents.allows_skill("w", "image_gen_v1"))

    def test_empty_whitelist_allows_nothing(self):
        self.make_agent("empty", {"tools": [], "skills": []})
        self.assertFalse(agents.allows_tool("empty", "get_time"))
        self.assertFalse(agents.allows_skill("empty", "writing"))

    def test_unknown_agent_uses_default_config(self):
        # 未建目录的 agent → 默认配置（不限制），而不是报错
        self.assertTrue(agents.allows_tool("ghost", "generate_image"))


class ListAgentsTest(AgentsTestBase):
    def test_default_agent_comes_first_then_sorted(self):
        self.make_agent("zeta", {"name": "Z"})
        self.make_agent("main", {"name": "默认"})
        self.make_agent("alpha")
        self.assertEqual([a["id"] for a in agents.list_agents()],
                         ["main", "alpha", "zeta"])

    def test_skips_files_and_illegal_dir_names(self):
        self.make_agent("ok")
        with open(os.path.join(self.root, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        os.makedirs(os.path.join(self.root, ".hidden"), exist_ok=True)
        self.assertEqual([a["id"] for a in agents.list_agents()], ["ok"])

    def test_display_name_falls_back_to_id(self):
        self.make_agent("noname")
        self.assertEqual(agents.list_agents()[0]["name"], "noname")

    def test_exposes_whitelists(self):
        self.make_agent("w", {"tools": ["read_file"], "skills": ["writing"]})
        item = [a for a in agents.list_agents() if a["id"] == "w"][0]
        self.assertEqual(item["tools"], ["read_file"])
        self.assertEqual(item["skills"], ["writing"])

    def test_empty_dir_returns_empty(self):
        self.assertEqual(agents.list_agents(), [])


class RevisionTest(AgentsTestBase):
    def test_changes_when_prompt_file_changes(self):
        d = self.make_agent("a", {"name": "A"}, prompt="第一版")
        before = agents.revision("a")
        with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
            f.write("第二版")
        _bump(os.path.join(d, "prompt.md"))
        self.assertNotEqual(agents.revision("a"), before)

    def test_changes_when_config_changes(self):
        d = self.make_agent("a", {"tools": ["read_file"]})
        before = agents.revision("a")
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            json.dump({"tools": ["write_file"]}, f)
        _bump(os.path.join(d, "agent.json"))
        self.assertNotEqual(agents.revision("a"), before)

    def test_stable_when_nothing_changes(self):
        self.make_agent("a", {"tools": ["read_file"]}, prompt="不变")
        self.assertEqual(agents.revision("a"), agents.revision("a"))


if __name__ == "__main__":
    unittest.main()
