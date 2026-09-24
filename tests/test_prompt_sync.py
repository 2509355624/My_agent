# -*- coding: utf-8 -*-
"""会话 system 头同步测试（app/agent_prompt.py::sync_session_system）。

要解决的真实问题：会话的 system 头是建立时写进 JSONL 的，之后一直躺在那。
改 prompt.md 后，进程内的 build_stable_prompt 立刻返回新内容，但**已有会话
读到的还是旧的那条**——网页端靠启动时重建，QQ 端连启动都不重建。结果就是
改完机器人的人设，已经在聊的群纹丝不动，只有新会话才生效。

这里验证三件事：变了要替换、没变一个字节都不动（保住前缀缓存）、
多条会话线之间互不影响。
"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import app.agents as agents
import app.memory as memory
import app.agent_prompt as prompt_mod


def _bump(path):
    """把 mtime 拨到未来，绕开文件系统精度导致的「看起来没变」。"""
    later = time.time() + 10
    os.utime(path, (later, later))


class SyncSessionSystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        # 配置 / 人设 / 已检查版本都是按目录或按 agent 缓存的，换目录必须清
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        prompt_mod.clear_session_cache()
        self.addCleanup(prompt_mod.clear_session_cache)

    # ─── 辅助 ───────────────────────────────────────

    def _mk_agent(self, aid="a", persona="你是A"):
        d = os.path.join(self.root, aid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        self._set_persona(aid, persona)

    def _set_persona(self, aid, text):
        path = os.path.join(self.root, aid, "prompt.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        _bump(path)

    def _seed(self, agent_id="a", session_key=None, system="旧的"):
        memory.save_history([{"role": "system", "content": system},
                             {"role": "user", "content": "你好"}],
                            agent_id, session_key)

    # ─── 用例 ───────────────────────────────────────

    def test_stale_header_is_replaced(self):
        self._mk_agent()
        self._seed()
        self.assertTrue(prompt_mod.sync_session_system("a"))
        h = memory.load_history("a")
        self.assertIn("你是A", h[0]["content"])
        # 非 system 消息原样保留
        self.assertEqual(h[1], {"role": "user", "content": "你好"})

    def test_persona_change_is_picked_up(self):
        self._mk_agent(persona="你是A")
        self._seed(system=prompt_mod.build_stable_prompt("a"))

        self._set_persona("a", "你是B")
        self.assertTrue(prompt_mod.sync_session_system("a"))

        first = memory.load_history("a")[0]["content"]
        self.assertIn("你是B", first)
        self.assertNotIn("你是A", first)

    def test_unchanged_header_leaves_the_file_untouched(self):
        """没变就一个字节都不写——system 在最前面，动它等于整段前缀缓存重算。"""
        self._mk_agent()
        self._seed(system=prompt_mod.build_stable_prompt("a"))
        path = agents.session_file("a")
        before = os.path.getmtime(path)

        self.assertFalse(prompt_mod.sync_session_system("a"))

        self.assertEqual(os.path.getmtime(path), before)

    def test_second_call_is_a_noop(self):
        self._mk_agent()
        self._seed()
        self.assertTrue(prompt_mod.sync_session_system("a"))
        self.assertFalse(prompt_mod.sync_session_system("a"))

    def test_session_without_system_header_gets_one(self):
        self._mk_agent()
        memory.save_history([{"role": "user", "content": "你好"}], "a")
        self.assertTrue(prompt_mod.sync_session_system("a"))
        self.assertEqual(memory.load_history("a")[0]["role"], "system")

    def test_missing_session_file_is_left_alone(self):
        """会话还没建过就不在这一处建头，交给 _ensure_system_prompt。"""
        self._mk_agent()
        self.assertFalse(prompt_mod.sync_session_system("a"))
        self.assertFalse(os.path.exists(agents.session_file("a")))

    def test_session_lines_are_independent(self):
        """一个 agent 挂多条会话线（QQ 的每个群各一条）：只动指定的那条。"""
        self._mk_agent(persona="你是A")
        stable = prompt_mod.build_stable_prompt("a")
        memory.save_history([{"role": "system", "content": stable}], "a", "group_1")
        memory.save_history([{"role": "system", "content": "过期的人设"}],
                            "a", "group_2")

        self.assertTrue(prompt_mod.sync_session_system("a", "group_2"))

        self.assertIn("你是A",
                      memory.load_history("a", "group_2")[0]["content"])
        # 另一个群没被动过
        self.assertEqual(len(memory.load_history("a", "group_1")), 1)


if __name__ == "__main__":
    unittest.main()
