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


class ModelConfigTest(AgentsTestBase):
    """agent 级模型配置：空串 = 继承 .env 的全局默认。"""

    def test_defaults_to_empty(self):
        self.make_agent("a", {})
        cfg = agents.agent_config("a")
        self.assertEqual(cfg["provider"], "")
        self.assertEqual(cfg["model"], "")

    def test_reads_configured_values(self):
        self.make_agent("a", {"provider": "volc", "model": "m1"})
        cfg = agents.agent_config("a")
        self.assertEqual(cfg["provider"], "volc")
        self.assertEqual(cfg["model"], "m1")

    def test_provider_lowercased(self):
        self.make_agent("a", {"provider": "VOLC"})
        self.assertEqual(agents.agent_config("a")["provider"], "volc")

    def test_unknown_provider_cleared(self):
        """写错的 provider 必须清空而不是原样保留，否则界面显示的和实际用的会不一致。"""
        self.make_agent("a", {"provider": "gpt5"})
        self.assertEqual(agents.agent_config("a")["provider"], "")

    def test_non_string_values_cleared(self):
        self.make_agent("a", {"provider": ["volc"], "model": 123})
        cfg = agents.agent_config("a")
        self.assertEqual(cfg["provider"], "")
        self.assertEqual(cfg["model"], "")

    def test_model_not_validated(self):
        """model 不做白名单校验，写错了应该带着报错暴露出来，而不是被悄悄清空。"""
        self.make_agent("a", {"provider": "volc", "model": "随便写的名字"})
        self.assertEqual(agents.agent_config("a")["model"], "随便写的名字")

    def test_legacy_config_unchanged(self):
        """老 agent.json 没有这两个字段，读出来必须是空串（等价于行为不变）。"""
        self.make_agent("a", {"tools": ["read_file"]})
        cfg = agents.agent_config("a")
        self.assertEqual(cfg["provider"], "")
        self.assertEqual(cfg["model"], "")
        self.assertEqual(cfg["tools"], ["read_file"])

    def test_list_agents_exposes_model_fields(self):
        self.make_agent("a", {"provider": "volc", "model": "m1"})
        item = [x for x in agents.list_agents() if x["id"] == "a"][0]
        self.assertEqual(item["provider"], "volc")
        self.assertEqual(item["model"], "m1")


class ContextBudgetTest(AgentsTestBase):
    """agent 级上下文预算：0 = 继承 .env 的 CONTEXT_BUDGET。"""

    def test_defaults_to_inherit(self):
        self.make_agent("a", {})
        self.assertEqual(agents.agent_config("a")["context_budget"], 0)

    def test_reads_configured_value(self):
        self.make_agent("a", {"context_budget": 64000})
        self.assertEqual(agents.agent_config("a")["context_budget"], 64000)

    def test_string_number_accepted(self):
        """手改 json 时写成字符串也该认，不该因此悄悄回退成全局值。"""
        self.make_agent("a", {"context_budget": "64000"})
        self.assertEqual(agents.agent_config("a")["context_budget"], 64000)

    def test_garbage_falls_back_to_inherit(self):
        """写错一律归 0（继承全局），而不是抛异常拦住整个 agent 加载。"""
        for i, bad in enumerate(("", "abc", None, [], {})):
            aid = "bad%d" % i
            self.make_agent(aid, {"context_budget": bad})
            self.assertEqual(agents.agent_config(aid)["context_budget"], 0, bad)

    def test_out_of_range_falls_back_to_inherit(self):
        """太小会每轮都摘要（反而更贵），太大等于没设——两者都归 0。"""
        cases = (100, agents.MIN_CONTEXT_BUDGET - 1,
                 agents.MAX_CONTEXT_BUDGET + 1, 10 ** 9)
        for i, bad in enumerate(cases):
            aid = "range%d" % i
            self.make_agent(aid, {"context_budget": bad})
            self.assertEqual(agents.agent_config(aid)["context_budget"], 0, bad)

    def test_boundaries_accepted(self):
        for i, good in enumerate((agents.MIN_CONTEXT_BUDGET,
                                  agents.MAX_CONTEXT_BUDGET)):
            aid = "ok%d" % i
            self.make_agent(aid, {"context_budget": good})
            self.assertEqual(agents.agent_config(aid)["context_budget"], good)

    def test_legacy_config_unchanged(self):
        """老 agent.json 没这个字段 → 0，等价于行为不变。"""
        self.make_agent("a", {"tools": ["read_file"]})
        cfg = agents.agent_config("a")
        self.assertEqual(cfg["context_budget"], 0)
        self.assertEqual(cfg["tools"], ["read_file"])

    def test_list_agents_exposes_budget(self):
        self.make_agent("a", {"context_budget": 64000})
        item = [x for x in agents.list_agents() if x["id"] == "a"][0]
        self.assertEqual(item["context_budget"], 64000)


class SaveAgentConfigTest(AgentsTestBase):
    """后台保存 agent.json。"""

    def test_preserves_fields_not_edited(self):
        """最要紧的一条：界面上只有 provider/model，回写不能把 tools/skills 冲掉。"""
        self.make_agent("a", {"name": "A", "tools": ["read_file"],
                              "skills": ["writing"], "prompt": "内联人设"})
        raw = agents.agent_raw_config("a")
        raw["provider"] = "volc"
        raw["model"] = "m1"
        self.assertTrue(agents.save_agent_config("a", raw))

        after = agents.agent_raw_config("a")
        self.assertEqual(after["tools"], ["read_file"])
        self.assertEqual(after["skills"], ["writing"])
        self.assertEqual(after["name"], "A")
        self.assertEqual(after["prompt"], "内联人设")
        self.assertEqual(after["provider"], "volc")

    def test_takes_effect_without_mtime_change(self):
        """写后立刻读必须看到新值——同秒内 mtime 可能不变，缓存要被主动清掉。"""
        self.make_agent("a", {})
        agents.agent_config("a")            # 先让缓存建立
        raw = agents.agent_raw_config("a")
        raw["provider"] = "deepseek"
        agents.save_agent_config("a", raw)
        self.assertEqual(agents.agent_config("a")["provider"], "deepseek")

    def test_creates_missing_dir(self):
        self.assertTrue(agents.save_agent_config("brandnew", {"provider": "volc"}))
        self.assertEqual(agents.agent_config("brandnew")["provider"], "volc")

    def test_raw_config_fallbacks(self):
        self.assertEqual(agents.agent_raw_config("nope"), {})
        self.assertEqual(agents.agent_raw_config("../evil"), {})

    def test_raw_config_on_broken_json(self):
        d = self.make_agent("a", {})
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        self.assertEqual(agents.agent_raw_config("a"), {})

    def test_rejects_bad_args(self):
        self.assertFalse(agents.save_agent_config("../evil", {}))
        self.assertFalse(agents.save_agent_config("a", "不是 dict"))


class SavePersonaTest(AgentsTestBase):
    def test_roundtrip(self):
        self.make_agent("a", {}, prompt="旧人设")
        self.assertTrue(agents.save_persona("a", "新人设"))
        self.assertEqual(agents.persona_text("a"), "新人设")

    def test_immediate_effect(self):
        self.make_agent("a", {}, prompt="旧")
        agents.persona_text("a")            # 先让缓存建立
        agents.save_persona("a", "新")
        self.assertEqual(agents.persona_text("a"), "新")

    def test_creates_file_when_absent(self):
        self.make_agent("a", {})
        self.assertTrue(agents.save_persona("a", "第一次写人设"))
        self.assertEqual(agents.persona_text("a"), "第一次写人设")

    def test_rejects_bad_args(self):
        self.assertFalse(agents.save_persona("../evil", "x"))
        self.assertFalse(agents.save_persona("a", 123))

    def test_prompt_file_traversal_blocked(self):
        """agent.json 里把 prompt_file 写成 ../outside.md 也不能指到目录外。"""
        self.make_agent("a", {"prompt_file": "../outside.md"})
        self.assertEqual(os.path.basename(agents.persona_path("a")), "prompt.md")
        agents.save_persona("a", "写这里")
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "outside.md")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "a", "prompt.md")))

    def test_persona_path_invalid_id(self):
        self.assertIsNone(agents.persona_path("../evil"))


if __name__ == "__main__":
    unittest.main()
