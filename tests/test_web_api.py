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

import app.agent as agent
import app.cancel as cancel_mod
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
        # 这些用例测的是「编辑重发怎么截断历史」，不关心 system 头同步。
        # 关掉它：种子历史里的 "S" 是假头，同步会把它当成「人设变了」重写成
        # 真实人设（那是另一套用例的事，放在 test_agent_prompt 里）。
        p = mock.patch.object(main, "sync_session_system", lambda *a, **k: False)
        p.start()
        self.addCleanup(p.stop)
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

    # ─── /api/stop（手动中断）────────────────────────

    def test_stop_without_request_id_is_rejected(self):
        self.assertEqual(self.client.post("/api/stop", json={}).status_code, 400)

    def test_stop_unknown_request_reports_miss(self):
        resp = self.client.post("/api/stop", json={"request_id": "never-started"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["hit"])

    def test_stop_interrupts_running_stream_and_flushes(self):
        """端到端：流跑到一半点停止。

        真实的 run_agent_stream 全程参与（只把 LLM 换成脚本），覆盖的是完整链路：
        main 建注册表 → generate 绑定线程 → agent 的三个检查点 → aborted 事件 →
        落盘 → 注册表摘除。把 run_agent_stream 整个 mock 掉就测不到这些。
        """
        rid = "rid-stop-test"

        def slow_llm(messages, provider=None, model=None, cancel_event=None):
            # 必须产出 reasoning：正文会被 agent 攒着等收完再解析工具调用，
            # 流上根本看不到中间状态，也就没机会在中途插入这条停止请求。
            for i in range(200):
                if cancel_event is not None and cancel_event.is_set():
                    return
                yield "reasoning", "思考第%d步 " % i

        with mock.patch.object(agent, "call_llm_stream", slow_llm):
            # 流是惰性执行的：generate() 只在被迭代时才跑，所以 patch 必须
            # 覆盖到把流读完为止，不能只包住 post。
            resp = self.client.post(
                "/api/chat",
                json={"message": "写一篇长文", "request_id": rid},
                buffered=False)
            self.assertEqual(resp.status_code, 200)

            it = iter(resp.response)
            self.assertEqual(json.loads(bytes(next(it)).decode("utf-8"))["type"], "user")
            next(it)                                 # 再吃掉一个 reasoning 块

            # 此刻 generate() 正挂在 yield 上——停在这里发停止请求
            hit = self.client.post("/api/stop", json={"request_id": rid})
            self.assertTrue(hit.get_json()["hit"])

            body = b"".join(list(it)).decode("utf-8")
            events = [json.loads(l) for l in body.split("\n") if l.strip()]
            self.assertEqual(events[-1]["type"], "aborted")

        saved = memory.load_history("main")
        self.assertTrue(any(m.get("tool_name") == "user_cancel" for m in saved))
        self.assertEqual(cancel_mod.active_count(), 0)   # 注册表已摘除

    def test_chat_without_request_id_skips_cancel_registry(self):
        # 旧客户端/脚本不带 request_id：不建键、不落任何状态，行为与从前一致
        with mock.patch.object(main, "run_agent_stream") as fake:
            fake.return_value = iter([{"type": "assistant", "content": "ok"}])
            self.client.post("/api/chat", json={"message": "你好"})
            self.assertIsNone(fake.call_args.kwargs.get("cancel_event"))
        self.assertEqual(cancel_mod.active_count(), 0)

    # ─── /api/chat ───────────────────────────────────

    def test_chat_rejects_empty_message(self):
        resp = self.client.post("/api/chat", json={"message": "   "})
        self.assertEqual(resp.status_code, 400)

    def test_chat_streams_ndjson_and_pre_executes_user_tool_block(self):
        captured = {}

        def fake_stream(user_input, history, provider=None, model=None,
                        pre_tool_results=None, agent_id=None, image=None,
                        **kwargs):
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

    # ─── /api/chat：事件级落盘 ───────────────────────

    @staticmethod
    def _visible(agent_id="main"):
        """磁盘上非 system 的消息内容（落盘结果的地面真值）。"""
        return [m.get("content") for m in memory.load_history(agent_id)
                if m.get("role") != "system"]

    def test_session_events_are_flushed_before_stream_ends(self):
        """流还没跑完，已经推出去的内容就该在盘上。

        否则后台仍在跑（生图可能阻塞数十分钟）时，另一个标签页刷新只能看到
        上一轮；改完 app/*.py 重启服务则整轮内容蒸发。
        """
        snapshots = []

        def fake_stream(user_input, history, provider=None, model=None,
                        pre_tool_results=None, agent_id=None, image=None,
                        **kwargs):
            history.append({"role": "user", "content": user_input})
            yield {"type": "user", "content": user_input}
            snapshots.append(self._visible())      # 生成器尚未结束，finally 也没跑

            history.append({"role": "assistant", "content": "A1"})
            yield {"type": "assistant", "content": "A1"}
            snapshots.append(self._visible())

            history.append({"role": "tool_result", "content": "T1",
                            "tool_name": "lookup"})
            yield {"type": "tool_result", "name": "lookup", "result": "T1"}
            snapshots.append(self._visible())

        with mock.patch.object(main, "run_agent_stream", fake_stream):
            resp = self.client.post("/api/chat", json={"message": "U1"})
            self.assertEqual(resp.status_code, 200)
            resp.get_data()                        # 消费完整个流

        self.assertEqual(snapshots, [["U1"], ["U1", "A1"], ["U1", "A1", "T1"]])

    def test_thinking_and_tool_call_events_are_not_persisted(self):
        """reasoning / tool_call 不入历史，也不该触发额外的落盘语义。"""
        def fake_stream(user_input, history, provider=None, model=None,
                        pre_tool_results=None, agent_id=None, image=None,
                        **kwargs):
            yield {"type": "reasoning", "content": "想想"}
            yield {"type": "tool_call", "name": "lookup", "args": {}}
            history.append({"role": "assistant", "content": "好了"})
            yield {"type": "assistant", "content": "好了"}

        with mock.patch.object(main, "run_agent_stream", fake_stream):
            resp = self.client.post("/api/chat", json={"message": "问一句"})
            resp.get_data()

        self.assertEqual(self._visible(), ["好了"])

    def test_client_disconnect_keeps_flushed_content(self):
        """客户端中途断开：已推送的内容必须已经在盘上。"""
        def fake_stream(user_input, history, provider=None, model=None,
                        pre_tool_results=None, agent_id=None, image=None,
                        **kwargs):
            history.append({"role": "user", "content": user_input})
            yield {"type": "user", "content": user_input}
            history.append({"role": "assistant", "content": "A1"})
            yield {"type": "assistant", "content": "A1"}
            history.append({"role": "assistant", "content": "A2"})
            yield {"type": "assistant", "content": "A2"}

        with mock.patch.object(main, "run_agent_stream", fake_stream):
            resp = self.client.post("/api/chat", json={"message": "U1"})
            it = iter(resp.response)
            next(it)                               # user
            next(it)                               # A1
            it.close()                             # 模拟断开，A2 从未产生

        self.assertEqual(self._visible(), ["U1", "A1"])

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
                        pre_tool_results=None, agent_id=None, image=None,
                        **kwargs):
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
                        pre_tool_results=None, agent_id=None, image=None,
                        **kwargs):
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

    # ─── 带图对话（图片只在本轮有效，不进历史）─────────

    def _post_with_image(self, message="识别这张图",
                         image="data:image/jpeg;base64,AAAA"):
        captured = {}

        def fake_stream(user_input, history, provider=None, model=None,
                        pre_tool_results=None, agent_id=None, image=None,
                        **kwargs):
            # 与真实循环一致：先入历史再出流（落盘契约依赖这个顺序）
            history.append({"role": "user", "content": user_input})
            captured["user_input"] = user_input
            captured["image"] = image
            captured["history"] = [m.get("content") for m in history]
            yield {"type": "user", "content": user_input}

        with mock.patch.object(main, "run_agent_stream", fake_stream):
            resp = self.client.post("/api/chat", json={
                "message": message, "image": image, "agent": "main"})
        return resp, captured

    def test_chat_forwards_image_to_agent_loop(self):
        image = "data:image/jpeg;base64," + "A" * 500
        resp, captured = self._post_with_image(image=image)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["image"], image)

    def test_chat_keeps_image_base64_out_of_history(self):
        image = "data:image/jpeg;base64," + "B" * 500
        _, captured = self._post_with_image(image=image)
        # 历史里只留一句占位文本
        self.assertIn("（用户上传了一张图片：识别这张图）", captured["history"])
        self.assertNotIn("B" * 50, "\n".join(captured["history"]))

        # 落盘后同样不能有——刷新/重启读到的是磁盘那一份
        with open(agents.session_file("main"), encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("（用户上传了一张图片：识别这张图）", raw)
        self.assertNotIn("B" * 50, raw)

    def test_chat_allows_image_without_text(self):
        resp, captured = self._post_with_image(message="")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["user_input"], "（用户上传了一张图片）")

    def test_chat_without_text_and_without_image_still_rejected(self):
        self.assertEqual(
            self.client.post("/api/chat", json={"message": "   "}).status_code, 400)

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


class AgentAdminApiTest(unittest.TestCase):
    """后台管理接口：读 agent 配置、保存人设与模型。

    这里验证的重点不是「能写进去」，而是**写的时候别把别的东西弄坏**——
    接口只暴露 prompt / provider / model 三项，其余字段必须原样留在磁盘上。
    """

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
        self.client = main.app.test_client()

    def _write_agent(self, aid, cfg=None, prompt=None):
        d = os.path.join(self.root, aid)
        os.makedirs(d, exist_ok=True)
        if cfg is not None:
            with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)
        if prompt is not None:
            with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
                f.write(prompt)
        return d

    def test_get_returns_detail(self):
        self._write_agent("main", {"name": "主", "description": "描述",
                                   "tools": ["read_file"]}, prompt="你是助手")
        d = self.client.get("/api/agent/main").get_json()
        self.assertEqual(d["id"], "main")
        self.assertEqual(d["name"], "主")
        self.assertEqual(d["description"], "描述")
        self.assertEqual(d["prompt"], "你是助手")
        self.assertEqual(d["prompt_file"], "prompt.md")
        # 没配就是空串，并由 *_effective 字段告诉界面实际会用谁
        self.assertEqual(d["provider"], "")
        self.assertEqual(d["global_provider"], main.LLM_PROVIDER)
        self.assertEqual(d["effective_provider"], main.LLM_PROVIDER)
        self.assertEqual(d["effective_model"], main.PROVIDERS[main.LLM_PROVIDER]["model"])
        self.assertEqual(d["tools"], ["read_file"])
        self.assertTrue(any(p["id"] == "volc" for p in d["providers"]))

    def test_put_saves_model_and_prompt(self):
        self._write_agent("main", {"tools": ["read_file"]}, prompt="旧人设")
        resp = self.client.put("/api/agent/main", json={
            "provider": "deepseek", "model": "m-x", "prompt": "新的人设"})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["effective_provider"], "deepseek")
        self.assertEqual(d["effective_model"], "m-x")
        self.assertEqual(d["prompt"], "新的人设")

        # 落盘检查：模型写进去了，而没在界面上暴露的 tools 原样保留
        raw = agents.agent_raw_config("main")
        self.assertEqual(raw["provider"], "deepseek")
        self.assertEqual(raw["model"], "m-x")
        self.assertEqual(raw["tools"], ["read_file"])
        with open(os.path.join(self.root, "main", "prompt.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "新的人设")

    def test_put_only_model_leaves_prompt_alone(self):
        self._write_agent("main", {}, prompt="别动我")
        self.client.put("/api/agent/main", json={"provider": "volc"})
        with open(os.path.join(self.root, "main", "prompt.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "别动我")

    def test_effective_model_falls_back_to_provider_default(self):
        """只选 provider 不填 model 时，实际用的是该家的默认模型。"""
        self._write_agent("main", {})
        d = self.client.put("/api/agent/main", json={
            "provider": "volc", "model": ""}).get_json()
        self.assertEqual(d["effective_provider"], "volc")
        self.assertEqual(d["effective_model"], main.PROVIDERS["volc"]["model"])

    def test_clearing_returns_to_global_default(self):
        self._write_agent("main", {"provider": "deepseek", "model": "m"})
        d = self.client.put("/api/agent/main", json={
            "provider": "", "model": ""}).get_json()
        self.assertEqual(d["provider"], "")
        self.assertEqual(d["effective_provider"], main.LLM_PROVIDER)

    def test_put_creates_config_when_absent(self):
        self._write_agent("newone")
        resp = self.client.put("/api/agent/newone", json={"provider": "volc"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.exists(os.path.join(self.root, "newone", "agent.json")))

    def test_put_rejects_unknown_provider(self):
        self._write_agent("main", {})
        self.assertEqual(
            self.client.put("/api/agent/main", json={"provider": "gpt5"}).status_code, 400)

    def test_put_rejects_wrong_types(self):
        self._write_agent("main", {})
        for body in ({"provider": 5}, {"model": ["x"]}, {"prompt": {"a": 1}}):
            self.assertEqual(
                self.client.put("/api/agent/main", json=body).status_code, 400, body)

    def test_put_rejects_non_object_body(self):
        self._write_agent("main", {})
        resp = self.client.put("/api/agent/main", data="不是 json",
                               content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_bad_agent_id_rejected(self):
        self.assertEqual(self.client.get("/api/agent/-bad").status_code, 400)

    def test_remote_blocked_by_default(self):
        self._write_agent("main", {})
        with mock.patch.object(main, "ADMIN_ALLOW_REMOTE", False):
            resp = self.client.put("/api/agent/main", json={"model": "x"},
                                   environ_base={"REMOTE_ADDR": "192.168.1.9"})
        self.assertEqual(resp.status_code, 403)

    def test_remote_allowed_when_opted_in(self):
        self._write_agent("main", {})
        with mock.patch.object(main, "ADMIN_ALLOW_REMOTE", True):
            resp = self.client.put("/api/agent/main", json={"provider": "volc"},
                                   environ_base={"REMOTE_ADDR": "192.168.1.9"})
        self.assertEqual(resp.status_code, 200)

    def test_admin_page_is_served(self):
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Agent", resp.data)


if __name__ == "__main__":
    unittest.main()
