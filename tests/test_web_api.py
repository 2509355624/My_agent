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
import app.comfy_status as comfy_status
import app.main as main


def _chain_all():
    """当前生效的全局降级链（.env 优先），[(pid, model), ...]。"""
    from app.llm import parse_chain
    return parse_chain(main.LLM_FALLBACK_CHAIN)


def _chain_head():
    c = _chain_all()
    if c:
        return c[0]
    return (main.LLM_PROVIDER, main.PROVIDERS[main.LLM_PROVIDER]["model"])
import app.agents as agents
import app.memory as memory
import app.tools.normal.documents as documents
from app.main import _safe_base_filename

# comfy_status.snapshot 一律吃内存快照，任何路径都不走真网络
# （状态栏已不再探测 ComfyUI，见 app/comfy_status 的模块注释）
_comfy_patch = None


def setUpModule():
    global _comfy_patch
    _comfy_patch = mock.patch.object(
        comfy_status, "snapshot",
        return_value={"online": True, "running": 0, "pending": 0, "ts": 0.0})
    _comfy_patch.start()


def tearDownModule():
    global _comfy_patch
    if _comfy_patch is not None:
        _comfy_patch.stop()
        _comfy_patch = None


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

        def slow_llm(messages, cancel_event=None, **kwargs):
            # 必须产出 reasoning：正文会被 agent 攒着等收完再解析工具调用，
            # 流上根本看不到中间状态，也就没机会在中途插入这条停止请求。
            # **kwargs 是刻意的：call_llm_stream 的签名会随功能加参数
            # （timeout / provider / model / require_vision…），桩函数硬列参数
            # 会在下次加参数时直接 TypeError，被测的链路根本没跑起来就"通过"
            # 或"失败"了（2026-10-03 就踩过一次 require_vision）。
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

    def test_chat_pins_the_selected_model(self):
        """网页端选谁就是谁：/api/chat 必须带 strict=True（不降级）。

        2026-10-03：面板选的模型原本只是链头，链尾会顶上——界面显示 ollama、
        实际是 deepseek 答的，账单和直觉对不上。网页端不要兜底。
        """
        with mock.patch.object(main, "run_agent_stream") as fake:
            fake.return_value = iter([{"type": "assistant", "content": "ok"}])
            self.client.post("/api/chat", json={
                "message": "你好", "provider": "ollama",
                "model": "qwen2.5:7b"})
            self.assertIs(fake.call_args.kwargs.get("strict"), True)

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
        # 没配就是空串，并由 *_effective 字段告诉界面实际会用谁。
        # 继承全局 = 走降级链（2026-10-04 用户定），生效值是链头而不是
        # 固定的 LLM_PROVIDER。
        self.assertEqual(d["provider"], "")
        self.assertEqual(d["global_provider"], main.LLM_PROVIDER)
        head_pid, head_model = _chain_head()
        self.assertEqual(d["effective_provider"], head_pid)
        self.assertEqual(d["effective_model"], head_model)
        # 降级链整条随 detail 下发（管理页要把「继承」的含义亮出来）
        self.assertEqual([c["pid"] for c in d["global_chain"]],
                         [pid for pid, _ in _chain_all()])
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
        # 清空 = 回到降级链链头（继承全局），不是固定的 LLM_PROVIDER
        self.assertEqual(d["effective_provider"], _chain_head()[0])

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

    def test_admin_page_has_bulk_persona_controls(self):
        """批量应用人设的控件必须在页面上。

        2026-10-04 改成**居中浮层**：作用范围（全部群聊 / 全部私聊 / 已勾选）
        在浮层里选，会话列表顶部只留「批量人设…」「清除人设…」两个入口。原先
        是列表顶上摆一排「全选 / 反选 / 全部群 / 全部私聊」，用户看不出在干
        什么；面板又渲染在中间列，点右列的按钮它弹在屏幕外。

        前端是纯 DOM 拼装、没有模板也没有构建步骤，删掉一个 id 只会让按钮
        **静默消失**、没有任何报错——所以在这里把 id 和函数名钉住。行为（范围
        算得对不对、发出去的 keys 对不对）由 tools/web_check.js 跑 jsdom 真验。
        """
        html = self.client.get("/admin").data.decode("utf-8")
        for el in ("sessBulkBar", "bulkApply", "bulkClear", "selCount",
                   "selNone", "bulkMask", "bulkModal"):
            self.assertIn('id="%s"' % el, html, "缺少批量人设控件：" + el)
        for fn in ("bulkRanges", "openBulkModal", "closeBulkModal",
                   "renderSelBar", "sessionsOfKind", "syncCheckboxes"):
            self.assertIn("function " + fn, html, "缺少批量人设函数：" + fn)
        self.assertIn("session_prompt_bulk", html)

    def test_admin_page_has_at_only_toggle(self):
        """群聊「只认 @」开关必须在页面上。

        2026-10-04 用户要求：某个群嫌它话多，必须点名才回。开关与「主动发言」
        并列在群行的「…」里——同样是纯 DOM 拼装，删掉只会静默消失。
        """
        html = self.client.get("/admin").data.decode("utf-8")
        self.assertIn("function toggleAtOnly", html, "缺「只认@」开关函数")
        self.assertIn("/at_only/", html, "缺「只认@」开关的后端路径")
        self.assertIn("只认@·开", html)
        self.assertIn("只认@·关", html)

    def test_admin_page_has_session_search_controls(self):
        """按 QQ 号/群名搜会话的控件必须在页面上。

        2026-10-03 用户提的：找一个 QQ 改人设要一条条翻。同样是纯 DOM 拼装、
        删个 id 只会让搜索框**静默消失**，所以把 id 和函数名钉住。

        ⚠️ 这里只钉「控件在」，不钉过滤行为——那是纯前端逻辑，没有 JS 运行时
        可跑；行为靠真无头浏览器冒烟验（见 2026-10-03 的记录）。
        """
        html = self.client.get("/admin").data.decode("utf-8")
        for el in ("sessSearchBar", "sessSearch", "sessSearchClear",
                   "sessSearchCount"):
            self.assertIn('id="%s"' % el, html, "缺少搜索控件：" + el)
        for fn in ("visibleSessions", "renderSearchBar", "applySearch"):
            self.assertIn("function " + fn, html, "缺少搜索函数：" + fn)

    def test_admin_page_has_three_column_session_layout(self):
        """会话列表独占右列；行内那排开关收进「…」。

        2026-10-03 用户提的：34 条会话要往下拖很久，左右却大片留白。改法是
        左（agent 列表）/ 中（设置卡片）/ 右（会话列表）三列，会话行压成两行
        文字，开关点行才展开。

        这里只钉「结构在」——同样是纯 DOM 拼装，把 id 或 class 改掉只会让右列
        **静默退回老样子**；展开行为靠真无头浏览器冒烟验。
        """
        html = self.client.get("/admin").data.decode("utf-8")
        self.assertIn('id="sessCol"', html, "缺右列容器")
        self.assertIn('class="sess-list" id="sessList"', html,
                      "会话列表要挂在右列里（class 也别改，CSS 靠它选中）")
        # 列表确实在右列**之后**——顺序反了就说明又被搬回中间那张卡片里了
        self.assertLess(html.index('id="sessCol"'), html.index('id="sessList"'))
        # 批量人设浮层必须在**右列之外**（挂在三列后面居中显示）。原先它塞在
        # 中间列里，而按钮在右列——中间列内容特别长，点完按钮浮层出现在屏幕外。
        start = html.index('id="sessCol"')
        end = html.index('</aside>', start)
        self.assertNotIn('id="bulkMask"', html[start:end],
                         "批量人设浮层不该塞回右列里")
        self.assertGreater(html.index('id="bulkMask"'), end,
                           "浮层要挂在三列之后，才能居中显示")
        for fn in ("buildTools", "toggleTools", "buildPromptEditor",
                   "sessSubText", "sessPromptLabel"):
            self.assertIn("function " + fn, html, "缺少函数：" + fn)


if __name__ == "__main__":
    unittest.main()
