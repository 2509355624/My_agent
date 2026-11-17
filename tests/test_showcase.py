# -*- coding: utf-8 -*-
"""公开产品展示页：独立会话、白名单生图、审核闸门与只读链路。"""

import os
import sys
import unittest
from unittest import mock

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.features.showcase import prompts as showcase_prompts
from app.features.showcase import routes as showcase_api  # noqa: E402


class _FakeDone:
    """对齐真实 image_jobs.Job：done 是 threading.Event，用 is_set() 判终态。"""

    def __init__(self, value=False):
        self._value = bool(value)

    def is_set(self):
        return self._value

    def set(self):
        self._value = True


class _FakeJob:
    def __init__(self, tag="HT-00000001"):
        self.tag = tag
        self.entry = None
        self.error = None
        self.done = _FakeDone(False)
        self.started = None


class ShowcaseTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True)
        self.app.register_blueprint(showcase_api.bp)
        self.client = self.app.test_client()
        self.headers = {"X-Friend-Token": "showcase-test-token"}
        secret = mock.patch.object(showcase_api.friend_auth, "secret",
                                   return_value="showcase-test-token")
        secret.start()
        self.addCleanup(secret.stop)
        with showcase_api._LOCK:
            showcase_api._SESSIONS.clear()
            showcase_api._JOBS.clear()
            showcase_api._STATS.update({
                "calls": 0, "total_tokens": 0, "hit_tokens": 0,
                "miss_tokens": 0, "last_call": None,
            })
        with showcase_api.llm._DEAD_LOCK:
            self._dead = dict(showcase_api.llm._DEAD)
            showcase_api.llm._DEAD.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        with showcase_api.llm._DEAD_LOCK:
            showcase_api.llm._DEAD.clear()
            showcase_api.llm._DEAD.update(self._dead)

    def test_gateway_token_is_mandatory(self):
        self.assertEqual(self.client.get("/api/showcase/config").status_code, 403)
        self.assertEqual(
            self.client.get("/api/showcase/config",
                            headers={"X-Friend-Token": "wrong"}).status_code,
            403)
        self.assertEqual(
            self.client.get("/api/showcase/config", headers=self.headers).status_code,
            200)

    def test_config_exposes_only_showcase_material(self):
        response = self.client.get("/api/showcase/config", headers=self.headers)
        data = response.get_json()
        self.assertEqual(data["role_card"]["id"], "a_hai")
        self.assertEqual(len(data["image_scenes"]), 3)
        self.assertNotIn("prompt", data["image_scenes"][0])
        self.assertEqual(data["comic"]["status"], "ready")
        self.assertTrue(all(p["asset_status"] == "ready"
                            for p in data["comic"]["panels"]))

    def test_comic_asset_serves_only_contracted_panels(self):
        """成品图接口只认契约里登记的分镜 id，且文件必须真实存在。"""
        response = self.client.get("/api/showcase/comic/asset/panel_9",
                                   headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "未知分镜")
        # 契约里每个 ready 的格子都必须有真实素材文件（目录 = web/showcase/comic）
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "web", "showcase", "comic")
        for p in showcase_prompts.COMIC_REPLAY["panels"]:
            path = os.path.join(root, p["id"] + ".jpg")
            self.assertTrue(os.path.isfile(path), path)

    def test_comic_asset_url_has_cache_bust_version(self):
        """换图靠 ?v= 版本号击穿缓存；素材接口不得再长缓存 1 小时。"""
        for p in showcase_prompts.COMIC_REPLAY["panels"]:
            url = p["image_url"]
            self.assertIn("?v=", url, url)
        response = self.client.get("/api/showcase/comic/asset/panel_1",
                                   headers=self.headers)
        self.assertEqual(response.status_code, 200)
        cache = response.headers.get("Cache-Control", "")
        self.assertIn("max-age=0", cache)
        response.close()

    def test_chat_isolated_and_returns_actual_injection_trace(self):
        def fake_build(line, query, k=None, day=None, context="", trace=None,
                       extra_lines=(), max_hits=None):
            trace.update({
                "query": query, "hit_count": 1,
                "hits": [{"id": "memory-1", "distance": 0.25,
                          "similarity": 0.75}],
                "selected": [{"id": "memory-1"}],
                "injected_count": 1,
            })
            return "【长期记忆】访客喜欢雨天"

        with mock.patch.object(showcase_api.persona_rag, "build_block",
                               side_effect=fake_build) as build, \
                mock.patch.object(showcase_api.persona_rag, "remember") as remember, \
                mock.patch.object(showcase_api.llm, "call_llm",
                                  return_value="雨天适合慢慢整理旧照片。"), \
                mock.patch.object(showcase_api.llm, "current_usage",
                                  return_value={"total_tokens": 100,
                                                "hit_tokens": 80,
                                                "miss_tokens": 20,
                                                "hit_rate": 0.8}):
            first = self.client.post(
                "/api/showcase/chat", headers=self.headers,
                json={"message": "我喜欢雨天", "mode": "guide"})
            self.assertEqual(first.status_code, 200)
            body = first.get_json()
            session_id = body["session_id"]
            self.assertEqual(body["trace"]["rag"]["injected_count"], 1)
            messages = body["trace"]["messages"]
            self.assertEqual(messages[1]["content"], "【长期记忆】访客喜欢雨天")
            self.assertEqual(messages[-1]["content"], "我喜欢雨天")
            self.assertTrue(build.call_args.args[0].startswith("showcase_"))
            self.assertTrue(remember.call_args.args[0].startswith("showcase_"))

            second = self.client.post(
                "/api/showcase/chat", headers=self.headers,
                json={"session_id": session_id, "message": "陪我逛逛书店",
                      "mode": "guide"})
            self.assertEqual(second.status_code, 200)
            # 会话自带「上次参观」的 12 条窗口，问完一轮仍是 12（_MAX_HISTORY 截断）
            self.assertEqual(second.get_json()["trace"]["history_count"], 12)
            self.assertEqual(second.get_json()["turn"], 2)

            other = self.client.post(
                "/api/showcase/chat", headers=self.headers,
                json={"message": "新访客你好", "mode": "guide"})
            self.assertEqual(other.status_code, 200)
            self.assertNotEqual(other.get_json()["session_id"], session_id)
            self.assertEqual(other.get_json()["trace"]["history_count"],
                             len(showcase_api._PRESET))

    def test_chat_rejects_bad_input_and_bad_mode(self):
        response = self.client.post(
            "/api/showcase/chat", headers=self.headers,
            json={"message": "", "mode": "guide"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/showcase/chat", headers=self.headers,
            json={"message": "你好", "mode": "internal"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/showcase/chat", headers=self.headers,
            json={"message": "你好", "session_id": "guest_shared"})
        self.assertEqual(response.status_code, 400)

    def test_chain_snapshot_has_cooldown_and_no_credentials(self):
        with mock.patch.object(showcase_api, "PERSONA_PROVIDER", "volc"), \
                mock.patch.object(showcase_api, "PERSONA_MODEL", "model-a"), \
                mock.patch.object(showcase_api, "LLM_FALLBACK_CHAIN", ""), \
                mock.patch.object(showcase_api.llm, "parse_chain",
                                  return_value=[("deepseek", "model-b")]):
            with showcase_api.llm._DEAD_LOCK:
                showcase_api.llm._DEAD[("volc", "model-a")] = \
                    __import__("time").time() + 600
            response = self.client.get("/api/showcase/chain", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["configured_head"]["state"], "cooldown")
        self.assertEqual(data["effective_head"]["provider"], "deepseek")
        self.assertEqual(data["configured_head"]["next_on_error"]["model"],
                         "model-b")
        self.assertEqual(len(data["blacklist"]), 1)
        text = response.get_data(as_text=True).lower()
        self.assertNotIn("api_key", text)
        self.assertNotIn("base_url", text)

    def test_image_submit_ignores_custom_prompt_and_enforces_owner(self):
        job = _FakeJob()
        with mock.patch.object(showcase_api, "build_t2i_workflow",
                               return_value={"1": {"class_type": "SaveImage"}}) as build, \
                mock.patch.object(showcase_api.image_jobs, "enqueue",
                                  return_value=(job, None)) as enqueue, \
                mock.patch.object(showcase_api.image_jobs, "queue_depth",
                                  return_value=4), \
                mock.patch.object(showcase_api.image_jobs, "ahead_of",
                                  return_value=3):
            response = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"scene_id": "rainy_bookstore", "prompt": "custom prompt"})
        self.assertEqual(response.status_code, 202)
        body = response.get_json()["job"]
        session_id = body["session_id"]
        self.assertEqual(body["trace"]["queue_position"], 3)
        self.assertEqual(body["trace"]["queue_depth"], 4)
        self.assertIn("cozy corner bookstore", build.call_args.args[1])
        self.assertNotEqual(build.call_args.args[1], "custom prompt")
        self.assertEqual(enqueue.call_args.kwargs["out_dir"], "web/showcase")
        self.assertTrue(enqueue.call_args.kwargs["quiet"])

        other_id, _other = showcase_api._session()
        denied = self.client.get(
            "/api/showcase/image/status",
            headers=self.headers,
            query_string={"tag": job.tag, "session_id": other_id})
        self.assertEqual(denied.status_code, 404)

    def test_custom_prompt_is_used_with_fixed_silver_channel(self):
        """显式 mode=custom：用户提示词原文送工作流，固定 silver，链路标明来源。"""
        job = _FakeJob()
        with mock.patch.object(showcase_api, "build_t2i_workflow",
                               return_value={"1": {"class_type": "SaveImage"}}) as build, \
                mock.patch.object(showcase_api.image_jobs, "enqueue",
                                  return_value=(job, None)) as enqueue:
            response = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"mode": "custom",
                      "prompt": "  a lonely lighthouse in a storm  "})
        self.assertEqual(response.status_code, 202)
        body = response.get_json()["job"]
        self.assertEqual(build.call_args.args[0], "silver")
        self.assertEqual(build.call_args.args[1], "a lonely lighthouse in a storm")
        self.assertEqual(enqueue.call_args.kwargs["skill"], "silver")
        self.assertEqual(body["scene"]["id"], "custom")
        self.assertIn("自定义", body["trace"]["prompt_contract"])
        self.assertTrue(body["trace"]["prompt"])

    def test_config_exposes_the_two_image_channels(self):
        """渠道白名单公开给页面：两个 id、默认 silver、场景只带后缀。

        `channel_suffix` 是给前端重拼文案用的（切渠道后卡片底部那行要变）；
        `channel` 给的是默认档文案，首屏直接可用。
        """
        data = self.client.get("/api/showcase/config", headers=self.headers).get_json()
        self.assertEqual([s["id"] for s in data["image_skills"]],
                         ["silver", "krea2-jiguang-1x"])
        self.assertEqual(data["default_image_skill"], "silver")
        self.assertEqual(data["image_scenes"][0]["channel"], "Silver · 现场生成")
        self.assertEqual(data["image_scenes"][0]["channel_suffix"], "现场生成")

    def test_channel_switch_drives_the_workflow(self):
        """访客切到 krea2：工作流渠道、入队 skill、链路文案三处都要跟着换。"""
        job = _FakeJob()
        with mock.patch.object(showcase_api, "build_t2i_workflow",
                               return_value={"1": {"class_type": "SaveImage"}}) as build, \
                mock.patch.object(showcase_api.image_jobs, "enqueue",
                                  return_value=(job, None)) as enqueue:
            response = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"scene_id": "rainy_bookstore", "skill": "krea2-jiguang-1x"})
        self.assertEqual(response.status_code, 202)
        body = response.get_json()["job"]
        self.assertEqual(build.call_args.args[0], "krea2-jiguang-1x")
        self.assertEqual(enqueue.call_args.kwargs["skill"], "krea2-jiguang-1x")
        self.assertEqual(body["scene"]["skill"], "krea2-jiguang-1x")
        self.assertIn("krea2 极光单次", body["scene"]["channel"])

    def test_unknown_channel_falls_back_to_silver(self):
        """请求体里的 skill 只能取白名单，别的值一律回落默认档。

        展示页是公网、没有登录——不能让人靠改请求体点名完整版极光、moss
        或任何内部渠道。回落而不是报错：页面文案变了、渠道没跟着变，也不能
        把访客卡在 400 上。
        """
        for bogus in ("krea2-jiguang", "moss", "", "   ", None, "../../etc/passwd"):
            with self.subTest(skill=bogus):
                with showcase_api._LOCK:
                    showcase_api._SESSIONS.clear()
                    showcase_api._JOBS.clear()
                job = _FakeJob()
                with mock.patch.object(showcase_api, "build_t2i_workflow",
                                       return_value={"1": {"class_type": "SaveImage"}}) as build, \
                        mock.patch.object(showcase_api.image_jobs, "enqueue",
                                          return_value=(job, None)):
                    response = self.client.post(
                        "/api/showcase/image/submit", headers=self.headers,
                        json={"mode": "custom", "prompt": "a cat", "skill": bogus})
                self.assertEqual(response.status_code, 202)
                self.assertEqual(build.call_args.args[0], "silver")

    def test_ai_expand_uses_the_selected_channel(self):
        """AI 扩写按访客选的渠道扩（krea2 要质量词开头的完整英文串）。"""
        with mock.patch("app.direct_gen._translate",
                        return_value={"prompt": "a cat, masterpiece"}) as translate, \
                mock.patch.object(showcase_api, "build_t2i_workflow",
                                  return_value={"1": {"class_type": "SaveImage"}}) as build, \
                mock.patch.object(showcase_api.image_jobs, "enqueue",
                                  return_value=(_FakeJob(), None)):
            response = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"mode": "ai", "prompt": "画只猫",
                      "skill": "krea2-jiguang-1x"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(translate.call_args.kwargs.get("skill"), "krea2-jiguang-1x")
        self.assertEqual(build.call_args.args[0], "krea2-jiguang-1x")

    def test_custom_prompt_requires_text_and_respects_length_cap(self):
        empty = self.client.post(
            "/api/showcase/image/submit", headers=self.headers,
            json={"mode": "custom", "prompt": "   "})
        self.assertEqual(empty.status_code, 400)
        self.assertIn("提示词", empty.get_json()["error"])
        too_long = self.client.post(
            "/api/showcase/image/submit", headers=self.headers,
            json={"mode": "custom", "prompt": "x" * 2001})
        self.assertEqual(too_long.status_code, 400)
        self.assertIn("2000", too_long.get_json()["error"])

    def test_predefined_scene_still_ignores_prompt_without_mode(self):
        """不传 mode=custom 时老规矩不变：只认白名单场景，prompt 仍被忽略。"""
        job = _FakeJob()
        with mock.patch.object(showcase_api, "build_t2i_workflow",
                               return_value={"1": {"class_type": "SaveImage"}}) as build, \
                mock.patch.object(showcase_api.image_jobs, "enqueue",
                                  return_value=(job, None)):
            response = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"scene_id": "rainy_bookstore", "prompt": "should be ignored"})
        self.assertEqual(response.status_code, 202)
        self.assertIn("cozy corner bookstore", build.call_args.args[1])
        self.assertNotEqual(build.call_args.args[1], "should be ignored")

    def test_ai_mode_uses_deepseek_expanded_prompt_and_keeps_original(self):
        """mode=ai：走大大怪同款扩写，用补出的英文提示词入队，原话留在链路。"""
        job = _FakeJob()
        expanded = ("a rainy old bookshop, warm amber light, wet stone "
                    "pavement, no people")
        with mock.patch("app.direct_gen._translate",
                        return_value={"skill": "silver", "prompt": expanded}) as translate, \
                mock.patch.object(showcase_api, "build_t2i_workflow",
                                  return_value={"1": {"class_type": "SaveImage"}}) as build, \
                mock.patch.object(showcase_api.image_jobs, "enqueue",
                                  return_value=(job, None)):
            response = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"mode": "ai", "prompt": "雨天的旧书店，暖光，没有人"})
        self.assertEqual(response.status_code, 202)
        body = response.get_json()["job"]
        self.assertEqual(translate.call_args.args[0], "雨天的旧书店，暖光，没有人")
        self.assertEqual(translate.call_args.kwargs["skill"], "silver")
        self.assertEqual(build.call_args.args[1], expanded)
        self.assertEqual(body["scene"]["id"], "ai")
        self.assertEqual(body["trace"]["original"], "雨天的旧书店，暖光，没有人")
        self.assertIn("DeepSeek", body["trace"]["prompt_contract"])

    def test_ai_mode_rejects_when_ai_does_not_produce_prompt(self):
        """AI 判成聊天 / 返回空 → 友好提示重来，绝不拿空 prompt 入队。"""
        with mock.patch("app.direct_gen._translate",
                        return_value={"reply": "这个我不会画"}), \
                mock.patch.object(showcase_api.image_jobs, "enqueue") as enqueue:
            response = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"mode": "ai", "prompt": "你好"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("写具体一点", response.get_json()["error"])
        enqueue.assert_not_called()

    def test_ai_mode_degrades_when_llm_fails(self):
        with mock.patch("app.direct_gen._translate",
                        side_effect=RuntimeError("llm down")):
            response = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"mode": "ai", "prompt": "雨天的书店"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("AI 扩写", response.get_json()["error"])

    def test_output_must_pass_audit_before_image_can_be_read(self):
        job = _FakeJob()
        with mock.patch.object(showcase_api, "build_t2i_workflow",
                               return_value={"1": {"class_type": "SaveImage"}}), \
                mock.patch.object(showcase_api.image_jobs, "enqueue",
                                  return_value=(job, None)):
            response = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"scene_id": "night_tram"})
        record = showcase_api._JOBS[job.tag]
        session_id = record["owner"]
        record["images"] = ["web/showcase/moss-qunyou/test.png"]
        verdict = showcase_api.image_audit.Verdict(
            False, category="other", failed=True, reason="audit unavailable")
        with mock.patch.object(showcase_api.comfy_out, "local_output_file",
                               return_value="test.png"), \
                mock.patch.object(showcase_api.image_audit, "check",
                                  return_value=verdict):
            showcase_api._audit_job(record)
        self.assertEqual(record["status"], "blocked")
        blocked = self.client.get(
            "/api/showcase/image", headers=self.headers,
            query_string={"tag": job.tag, "session_id": session_id})
        self.assertEqual(blocked.status_code, 409)

        record["audit"] = {"state": "pending", "allow": None}
        passed = showcase_api.image_audit.Verdict(
            True, category="safe", failed=False, reason="")
        with mock.patch.object(showcase_api.comfy_out, "local_output_file",
                               return_value="test.png"), \
                mock.patch.object(showcase_api.image_audit, "check",
                                  return_value=passed):
            showcase_api._audit_job(record)
        self.assertEqual(record["status"], "ready")

    def test_only_predefined_image_scene_is_accepted(self):
        response = self.client.post(
            "/api/showcase/image/submit", headers=self.headers,
            json={"scene_id": "internal_card", "prompt": "anything"})
        self.assertEqual(response.status_code, 400)

    def test_finished_old_job_does_not_block_resubmission(self):
        first = _FakeJob("HT-00000001")
        second = _FakeJob("HT-00000002")
        with mock.patch.object(showcase_api, "build_t2i_workflow",
                               return_value={"1": {"class_type": "SaveImage"}}), \
                mock.patch.object(showcase_api.image_jobs, "enqueue",
                                  side_effect=[(first, None), (second, None)]):
            response = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"scene_id": "rainy_bookstore"})
            session_id = response.get_json()["job"]["session_id"]
            first.error = RuntimeError("already finished")
            retry = self.client.post(
                "/api/showcase/image/submit", headers=self.headers,
                json={"scene_id": "rainy_bookstore", "session_id": session_id})
        self.assertEqual(retry.status_code, 202)
        self.assertEqual(retry.get_json()["job"]["tag"], "HT-00000002")

    def test_role_card_is_the_aquarium_guide(self):
        """展示页只有海洋馆讲解员一种玩法，旧的日常 / 末日模式必须清干净。"""
        prompts = showcase_api.showcase_prompts
        self.assertEqual(prompts.ROLE_CARD["name"], "阿海")
        self.assertEqual([m["id"] for m in prompts.PLAY_MODES], ["guide"])
        system = prompts.roleplay_system("guide")
        self.assertIn("讲解员", system)
        self.assertNotIn("丧尸", system)
        self.assertNotIn("岚灯", system)
        # 未知玩法回落默认，不抛错也不切换世界观
        self.assertEqual(prompts.roleplay_system("zombie_survival"),
                         prompts.roleplay_system("guide"))

    def test_guide_state_preloads_last_visit_window(self):
        entries = [
            {"id": "raw-1", "content": "她：欢迎来到海洋馆",
             "metadata": {"kind": "raw", "turn": 1}},
            {"id": "batch-1", "content": "批次叙事", "metadata": {"kind": "batch", "seq": 1}},
        ]
        fake = mock.Mock()
        fake.is_ready.return_value = True
        fake.list_entries.return_value = {"ok": True, "entries": entries}
        with mock.patch.object(showcase_api, "_rag_client", return_value=fake):
            response = self.client.get("/api/showcase/guide/state", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        # 新访客一进来就带着「上次参观」的 6 轮，不是空窗口
        self.assertEqual(data["preset_turns"], 6)
        self.assertEqual(data["window"]["count"], len(showcase_api._PRESET))
        self.assertEqual(data["window"]["messages"][0]["role"], "user")
        self.assertEqual(data["kb"]["counts"]["raw"], 1)
        self.assertEqual(data["kb"]["counts"]["batch"], 1)
        self.assertEqual(data["kb"]["counts"]["total"], 2)

    def test_guide_library_filters_kind_and_pages(self):
        entries = [{"id": "r%d" % i, "content": "第 %d 条" % i,
                    "metadata": {"kind": "raw", "turn": i}} for i in range(1, 6)]
        entries.append({"id": "f1", "content": "一条事实",
                        "metadata": {"kind": "fact", "turn": 1}})
        fake = mock.Mock()
        fake.is_ready.return_value = True
        fake.list_entries.return_value = {"ok": True, "entries": entries}
        with mock.patch.object(showcase_api, "_rag_client", return_value=fake):
            response = self.client.get(
                "/api/showcase/guide/library?line=kb&kind=raw&offset=1&limit=2",
                headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total"], 5)
        self.assertEqual([item["turn"] for item in data["entries"]], [2, 3])
        self.assertTrue(all(item["kind"] == "raw" for item in data["entries"]))

    def test_guide_library_labels_batch_and_fact_with_turn_span(self):
        """批次叙事和原子事实只有 turn_start/turn_end，不能显示成「第 ? 轮」。"""
        entries = [
            {"id": "batch-6-10-0", "content": "第二批",
             "metadata": {"kind": "batch", "turn_start": 6, "turn_end": 10}},
            {"id": "batch-1-5-0", "content": "第一批",
             "metadata": {"kind": "batch", "turn_start": 1, "turn_end": 5}},
            {"id": "1-5-0", "content": "一条事实",
             "metadata": {"kind": "fact", "turn_start": 1, "turn_end": 5}},
        ]
        fake = mock.Mock()
        fake.is_ready.return_value = True
        fake.list_entries.return_value = {"ok": True, "entries": entries}
        with mock.patch.object(showcase_api, "_rag_client", return_value=fake):
            data = self.client.get("/api/showcase/guide/library?line=kb",
                                   headers=self.headers).get_json()
        self.assertEqual([item["id"] for item in data["entries"]],
                         ["batch-1-5-0", "batch-6-10-0", "1-5-0"])
        for item in data["entries"]:
            self.assertTrue(item["turn"], item["id"])
            self.assertTrue(item["turn_start"])
        self.assertEqual(data["entries"][0]["turn_end"], 5)
        self.assertEqual(data["entries"][1]["turn"], 6)

    def test_guide_search_flags_threshold_and_sorts(self):
        fake = mock.Mock()
        fake.is_ready.return_value = True
        fake.search.return_value = {"ok": True, "docs": [
            {"id": "far", "content": "远的", "distance": 0.95,
             "metadata": {"kind": "raw"}},
            {"id": "near", "content": "近的", "distance": 0.31,
             "metadata": {"kind": "raw"}},
        ]}
        with mock.patch.object(showcase_api, "_rag_client", return_value=fake):
            response = self.client.post("/api/showcase/guide/search",
                                        headers=self.headers,
                                        json={"query": "会发光的鱼", "k": 5})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual([hit["id"] for hit in data["hits"]], ["near", "far"])
        self.assertTrue(data["hits"][0]["passed"])
        self.assertFalse(data["hits"][1]["passed"])
        self.assertEqual(data["hits"][0]["line"], "kb")

    def test_guide_search_rejects_empty_query(self):
        response = self.client.post("/api/showcase/guide/search",
                                    headers=self.headers, json={"query": ""})
        self.assertEqual(response.status_code, 400)

    def test_transcript_covers_the_whole_route(self):
        """母本 250 轮必须能整段翻出来。

        2026-11-16 用户反馈：页面「现场对话」只显示窗口里那 12 条，剩下两百多轮
        藏在库里，用户以为「走了 250 轮」是假的。这条守住「能翻到全部」。
        """
        response = self.client.get("/api/showcase/guide/transcript?limit=5",
                                   headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total_turns"], 250)
        self.assertEqual(data["total_groups"], 50)
        self.assertEqual(len(data["groups"]), 5)
        first = data["groups"][0]
        self.assertEqual(first["turn_start"], 1)
        self.assertEqual(first["turn_end"], 5)
        # 站名要从讲解词里剥掉「前面就是」这类引导语
        self.assertEqual(first["name"], "大洋池·蓝鲸展区")
        self.assertEqual(len(first["turns"]), 5)
        self.assertEqual(first["turns"][0]["role"], "guide")

    def test_transcript_pages_do_not_overlap(self):
        a = self.client.get("/api/showcase/guide/transcript?offset=0&limit=3",
                            headers=self.headers).get_json()
        b = self.client.get("/api/showcase/guide/transcript?offset=3&limit=3",
                            headers=self.headers).get_json()
        self.assertEqual([g["no"] for g in a["groups"]], [1, 2, 3])
        self.assertEqual([g["no"] for g in b["groups"]], [4, 5, 6])

    def test_chat_honours_recall_cap(self):
        """页面选了「5 条」就得按 5 条召回，别只在前端画个按钮。"""
        seen = {}

        def fake_build(line, query, k=None, day=None, context="", trace=None,
                       extra_lines=(), max_hits=None):
            seen["max_hits"] = max_hits
            trace.update({"hit_count": 0, "hits": [], "selected": [],
                          "injected_count": 0})
            return ""

        with mock.patch.object(showcase_api.persona_rag, "build_block",
                               side_effect=fake_build), \
                mock.patch.object(showcase_api.persona_rag, "remember"), \
                mock.patch.object(showcase_api.llm, "call_llm",
                                  return_value="深海区那只是鮟鱇。"), \
                mock.patch.object(showcase_api.llm, "current_usage",
                                  return_value={"total_tokens": 10}):
            response = self.client.post(
                "/api/showcase/chat", headers=self.headers,
                json={"message": "深海区那个亮着的是什么", "mode": "guide", "top": 5})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen["max_hits"], 5)
        self.assertEqual(response.get_json()["recall_top"], 5)

    def test_recall_cap_is_clamped(self):
        """越界值不许透传给检索：1 条没法回答，100 条会撑爆上下文。"""
        seen = {}

        def fake_build(line, query, k=None, day=None, context="", trace=None,
                       extra_lines=(), max_hits=None):
            seen["max_hits"] = max_hits
            trace.update({"hit_count": 0, "hits": [], "selected": [],
                          "injected_count": 0})
            return ""

        with mock.patch.object(showcase_api.persona_rag, "build_block",
                               side_effect=fake_build), \
                mock.patch.object(showcase_api.persona_rag, "remember"), \
                mock.patch.object(showcase_api.llm, "call_llm", return_value="嗯。"), \
                mock.patch.object(showcase_api.llm, "current_usage",
                                  return_value={"total_tokens": 10}):
            self.client.post("/api/showcase/chat", headers=self.headers,
                             json={"message": "你好", "mode": "guide", "top": 100})
        self.assertEqual(seen["max_hits"], 20)


class GuidePageLayoutTest(unittest.TestCase):
    """页面侧：右栏改面板切换、历史能翻全、召回上限能点（2026-11-16）。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "web", "showcase.html"),
                encoding="utf-8") as f:
            cls.src = f.read()

    def test_side_panels_switch_instead_of_scrolling(self):
        self.assertIn('data-pane="recall"', self.src)
        self.assertIn('data-pane="prompt"', self.src)
        self.assertIn('data-pane="library"', self.src)
        self.assertIn("function switchPane", self.src)
        # 一次只露一个：四个面板不能同时 is-active
        self.assertEqual(self.src.count('class="panel guide-panel is-active"'), 1)

    def test_transcript_is_reachable_from_the_chat_head(self):
        self.assertIn('id="btnTranscript"', self.src)
        self.assertIn('id="transcriptDialog"', self.src)
        self.assertIn("/api/showcase/guide/transcript", self.src)

    def test_recall_cap_is_explained_and_adjustable(self):
        self.assertIn('data-top="5"', self.src)
        self.assertIn("state.recallTop", self.src)
        # 必须写清「召回不花 token，进提示词才算」，否则用户以为按条数烧钱
        self.assertIn("不花 token", self.src)

    def test_library_explains_what_the_377_entries_are(self):
        self.assertIn("不是 377 轮对话", self.src)
        self.assertIn("原文 284 块", self.src)

    def test_library_never_prints_a_placeholder_turn(self):
        """批次/事实没有 `turn`，旧代码就渲染成「第 ? 轮」（2026-11-16 反馈）。"""
        self.assertIn("function turnLabel", self.src)
        self.assertIn("entry.turn_end", self.src)
        self.assertNotIn('entry.turn || "?"', self.src)


if __name__ == "__main__":
    unittest.main()
