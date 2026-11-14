# -*- coding: utf-8 -*-
"""公开产品展示页：独立会话、白名单生图、审核闸门与只读链路。"""

import os
import sys
import unittest
from unittest import mock

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import showcase_api  # noqa: E402


class _FakeJob:
    def __init__(self, tag="HT-00000001"):
        self.tag = tag
        self.entry = None
        self.error = None
        self.done = False
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
        self.assertEqual(data["role_card"]["id"], "lan_deng")
        self.assertEqual(len(data["image_scenes"]), 3)
        self.assertNotIn("prompt", data["image_scenes"][0])
        self.assertEqual(data["comic"]["status"], "assets_pending")
        self.assertTrue(all(p["image_url"] is None
                            for p in data["comic"]["panels"]))

    def test_chat_isolated_and_returns_actual_injection_trace(self):
        def fake_build(line, query, k=None, day=None, context="", trace=None):
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
                json={"message": "我喜欢雨天", "mode": "daily"})
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
                      "mode": "zombie_survival"})
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.get_json()["trace"]["history_count"], 2)
            self.assertEqual(second.get_json()["turn"], 2)

            other = self.client.post(
                "/api/showcase/chat", headers=self.headers,
                json={"message": "新访客你好", "mode": "daily"})
            self.assertEqual(other.status_code, 200)
            self.assertNotEqual(other.get_json()["session_id"], session_id)
            self.assertEqual(other.get_json()["trace"]["history_count"], 0)

    def test_chat_rejects_bad_input_and_bad_mode(self):
        response = self.client.post(
            "/api/showcase/chat", headers=self.headers,
            json={"message": "", "mode": "daily"})
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


if __name__ == "__main__":
    unittest.main()
