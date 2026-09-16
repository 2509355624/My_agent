# -*- coding: utf-8 -*-
"""Flask Web API 测试（app/main.py）。

用 Flask 自带的 test_client，不启动真实端口、不发真实网络请求。
重点：
- 文件名净化（_safe_base_filename）与文档路径沙箱
- /api/chat 的 NDJSON 流式返回 + 用户手输工具块的预执行注入
- 会话相关路由不污染真实 data/session.jsonl（setUp 里全部重定向到临时目录）
"""

import io
import json
import os
import tempfile
import unittest
from unittest import mock

import app.main as main
import app.agents as agents
import app.memory as memory
import app.tools.normal.documents as documents
from app.main import _safe_base_filename


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        docs_dir = os.path.join(self.tmp.name, "documents")
        targets = (
            (agents, "AGENTS_DIR", os.path.join(self.tmp.name, "agents")),
            (main, "DOCUMENTS_DIR", docs_dir),
            (documents, "DOCUMENTS_DIR", docs_dir),
        )
        for module, attr, value in targets:
            p = mock.patch.object(module, attr, value)
            p.start()
            self.addCleanup(p.stop)
        # agent 配置/人设有 mtime 缓存，换目录后必须清一次
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        self.docs_dir = docs_dir
        self.client = main.app.test_client()

    # ─── providers / history / clear ─────────────────

    def test_providers_endpoint(self):
        resp = self.client.get("/api/providers")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        ids = {p["id"] for p in data["providers"]}
        self.assertTrue({"volc", "deepseek", "ollama"}.issubset(ids))
        self.assertIn("default", data)

    def test_history_starts_empty(self):
        resp = self.client.get("/api/history")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["messages"], [])

    def test_clear_resets_non_system_messages(self):
        resp = self.client.post("/api/clear")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/api/history").get_json()["messages"], [])

    # ─── /api/chat ───────────────────────────────────

    def test_chat_rejects_empty_message(self):
        resp = self.client.post("/api/chat", json={"message": "   "})
        self.assertEqual(resp.status_code, 400)

    def test_chat_streams_ndjson_and_pre_executes_user_tool_block(self):
        captured = {}

        def fake_stream(user_input, history, provider=None, model=None,
                        pre_tool_results=None, agent_id=None):
            captured["user_input"] = user_input
            captured["pre"] = pre_tool_results
            captured["provider"] = provider
            captured["agent"] = agent_id
            yield {"type": "assistant", "content": "done"}

        with mock.patch.object(main, "run_agent_stream", fake_stream), \
                mock.patch.object(main, "execute_tool",
                                  lambda name, args: "结果:" + name):
            resp = self.client.post("/api/chat", json={
                "message": "查一下 [[TOOL:get_time]][[/TOOL]]",
                "provider": "volc", "model": "deepseek-v4-pro-ga-260813",
            })

        self.assertEqual(resp.status_code, 200)
        self.assertIn("ndjson", resp.headers["Content-Type"])
        events = [json.loads(ln) for ln in resp.get_data(as_text=True).strip().split("\n") if ln]
        self.assertEqual(events[-1], {"type": "assistant", "content": "done"})
        # 用户消息里的工具块应被剥离后作为输入，工具结果预先注入历史
        self.assertEqual(captured["user_input"], "查一下")
        self.assertEqual(captured["pre"],
                         [{"name": "get_time", "result": "结果:get_time"}])
        self.assertEqual(captured["provider"], "volc")
        self.assertEqual(captured["agent"], "main")   # 未指定 agent → 兜底到默认

    # ─── /api/chat：编辑某条用户消息后重发 ────────────

    def _seed_history(self, agent_id="main"):
        """造一段"问了三轮"的历史：用户/助手/工具结果混排。"""
        memory.save_history([
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "tool_result", "content": "T1"},
            {"role": "user", "content": "U2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "U3"},
            {"role": "assistant", "content": "A3"},
        ], agent_id)

    def _post_edit_capture(self, user_index, message="改过的话"):
        """发一次"编辑重发"，返回传给 agent 循环的历史（只留 content）。"""
        captured = {}

        def fake_stream(user_input, history, provider=None, model=None,
                        pre_tool_results=None, agent_id=None):
            captured["history"] = [m.get("content") for m in history]
            captured["user_input"] = user_input
            yield {"type": "assistant", "content": "done"}

        with mock.patch.object(main, "run_agent_stream", fake_stream):
            resp = self.client.post("/api/chat", json={
                "message": message, "edit_user_index": user_index,
                "agent": "main"})
        return resp, captured

    def test_edit_middle_message_drops_everything_after_it(self):
        self._seed_history()
        resp, captured = self._post_edit_capture(2)
        self.assertEqual(resp.status_code, 200)
        # 第 2 条用户消息之前的保留（含夹在中间的工具结果），之后的全部丢弃；
        # 被丢的 A2 / U3 / A3 都是在回应旧的那一句，留着会自相矛盾。
        self.assertEqual(captured["history"], ["S", "U1", "A1", "T1"])
        self.assertEqual(captured["user_input"], "改过的话")

    def test_edit_first_message_keeps_only_system(self):
        self._seed_history()
        resp, captured = self._post_edit_capture(1)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["history"], ["S"])

    def test_edit_last_message_keeps_earlier_turns(self):
        self._seed_history()
        resp, captured = self._post_edit_capture(3)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["history"],
                         ["S", "U1", "A1", "T1", "U2", "A2"])

    def test_edit_index_beyond_history_is_rejected(self):
        self._seed_history()
        resp, _ = self._post_edit_capture(99)
        # 找不到就别静默按原样追加——那会让人以为改动生效了
        self.assertEqual(resp.status_code, 400)

    def test_edit_index_must_be_numeric(self):
        self._seed_history()
        resp, _ = self._post_edit_capture("abc")
        self.assertEqual(resp.status_code, 400)

    def test_edit_index_invalid_leaves_history_untouched(self):
        self._seed_history()
        self._post_edit_capture(99)
        kept = [m.get("content") for m in memory.load_history("main")]
        self.assertEqual(kept, ["S", "U1", "A1", "T1", "U2", "A2", "U3", "A3"])

    def test_normal_send_keeps_full_history(self):
        self._seed_history()
        captured = {}

        def fake_stream(user_input, history, provider=None, model=None,
                        pre_tool_results=None, agent_id=None):
            captured["history"] = [m.get("content") for m in history]
            yield {"type": "assistant", "content": "done"}

        with mock.patch.object(main, "run_agent_stream", fake_stream):
            self.client.post("/api/chat", json={"message": "U4", "agent": "main"})
        # 不带 edit_user_index 时必须什么都不动
        self.assertEqual(captured["history"],
                         ["S", "U1", "A1", "T1", "U2", "A2", "U3", "A3"])

    def test_truncate_at_user_handles_bad_index_types(self):
        hist = [{"role": "system", "content": "S"},
                {"role": "user", "content": "U1"}]
        for bad in (0, -3, "1", None, 1.5):
            out, hit = main._truncate_at_user(hist, bad)
            self.assertFalse(hit, "index=%r 不该命中" % bad)
            self.assertEqual(out, hist)

    # ─── documents API ───────────────────────────────

    def test_documents_list_empty(self):
        resp = self.client.get("/api/documents")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["files"], [])

    def test_upload_read_delete_roundtrip(self):
        resp = self.client.post("/api/documents/upload", data={
            "file": (io.BytesIO("第一行\n第二行\n".encode("utf-8")), "笔记.txt"),
        }, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["name"], "笔记.txt")
        self.assertEqual(resp.get_json()["lines"], 2)

        listed = self.client.get("/api/documents").get_json()["files"]
        self.assertEqual([f["name"] for f in listed], ["笔记.txt"])

        content = self.client.get("/api/documents/笔记.txt").get_json()["content"]
        self.assertIn("第一行", content)

        self.assertEqual(self.client.delete("/api/documents/笔记.txt").status_code, 200)
        self.assertEqual(self.client.get("/api/documents").get_json()["files"], [])

    def test_delete_missing_document_returns_404(self):
        self.assertEqual(
            self.client.delete("/api/documents/ghost.txt").status_code, 404)

    def test_delete_rejects_path_outside_documents(self):
        resp = self.client.delete("/api/documents/..%2F..%2Fsecret.txt")
        # 文件名被净化后落在 documents 内，但那文件不存在 → 404（绝不是 200）
        self.assertIn(resp.status_code, (400, 404))


class SafeBaseFilenameTest(unittest.TestCase):
    def test_strips_directory_components(self):
        self.assertEqual(_safe_base_filename("../../etc/passwd"), "passwd")
        self.assertEqual(_safe_base_filename(r"..\..\windows\system32\cmd.exe"), "cmd.exe")

    def test_removes_dangerous_characters(self):
        self.assertEqual(_safe_base_filename('a<>:"|?*b.txt'), "ab.txt")

    def test_keeps_chinese_and_safe_punctuation(self):
        self.assertEqual(_safe_base_filename("面试-笔记_v1.md"), "面试-笔记_v1.md")

    def test_blank_becomes_unnamed(self):
        self.assertEqual(_safe_base_filename(""), "unnamed.txt")
        self.assertEqual(_safe_base_filename("   ...   "), "unnamed.txt")

    def test_strips_leading_dots(self):
        self.assertEqual(_safe_base_filename("...hidden.txt"), "hidden.txt")


if __name__ == "__main__":
    unittest.main()
