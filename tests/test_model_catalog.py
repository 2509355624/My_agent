# -*- coding: utf-8 -*-
"""模型清单测试（app/model_catalog.py 与 /api/models 的 provider 分支）。

这个模块的存在理由就是「清单不能写死」：模型 ID 会下线也会改名，写死的清单会
静默指向不存在的模型。所以解析逻辑（哪些该丢、哪些要标出来）是主要被测对象，
网络调用一律 mock。
"""

import unittest
from unittest import mock

import app.main as main
import app.model_catalog as mc


class ParseTest(unittest.TestCase):
    """各 provider 响应体的解析。都是纯函数，直接喂假数据。"""

    # ─── 火山方舟 ────────────────────────────────────

    def test_ark_drops_shutdown(self):
        rows = [
            {"id": "dead-260101", "status": "Shutdown", "task_type": ["TextGeneration"]},
            {"id": "alive-260901", "task_type": ["TextGeneration"]},
        ]
        ids = [m["id"] for m in mc._from_ark(rows)]
        self.assertEqual(ids, ["alive-260901"])

    def test_ark_keeps_only_text_generation(self):
        # 向量/生图/视频混进来只会误导：选中必然用不了
        rows = [
            {"id": "emb-1", "task_type": ["TextEmbedding"]},
            {"id": "t2i-1", "task_type": ["TextToImage"]},
            {"id": "chat-1", "task_type": ["TextGeneration"]},
            {"id": "misc-1"},                      # 没标任务类型，不敢认
        ]
        self.assertEqual([m["id"] for m in mc._from_ark(rows)], ["chat-1"])

    def test_ark_marks_retiring(self):
        rows = [
            {"id": "soon-260425", "status": "Retiring", "task_type": ["TextGeneration"]},
            {"id": "now-260731", "task_type": ["TextGeneration"]},
        ]
        out = mc._from_ark(rows)
        self.assertEqual(out[0]["id"], "now-260731")     # 在用的排前面
        self.assertFalse(out[0]["retiring"])
        self.assertTrue(out[1]["retiring"])
        self.assertEqual(out[1]["id"], "soon-260425")

    def test_ark_vision_from_visual_qa(self):
        rows = [
            {"id": "text-only", "task_type": ["TextGeneration"]},
            {"id": "can-see", "task_type": ["VisualQuestionAnswering", "TextGeneration"]},
        ]
        by_id = {m["id"]: m for m in mc._from_ark(rows)}
        self.assertFalse(by_id["text-only"]["vision"])
        self.assertTrue(by_id["can-see"]["vision"])

    def test_ark_ignores_junk_and_dedupes(self):
        rows = [
            None, "不是字典", {"no_id": 1},
            {"id": "dup", "task_type": ["TextGeneration"]},
            {"id": "dup", "task_type": ["TextGeneration"]},
            {"id": "  ", "task_type": ["TextGeneration"]},
        ]
        self.assertEqual([m["id"] for m in mc._from_ark(rows)], ["dup"])

    # ─── OpenAI 兼容（DeepSeek 官方）──────────────────

    def test_openai_vision_from_input_modalities(self):
        rows = [
            {"id": "deepseek-flash", "input_modalities": ["text", "image"]},
            {"id": "deepseek-v4-pro", "input_modalities": ["text"]},
        ]
        by_id = {m["id"]: m for m in mc._from_openai(rows)}
        self.assertTrue(by_id["deepseek-flash"]["vision"])
        self.assertFalse(by_id["deepseek-v4-pro"]["vision"])

    def test_openai_missing_modalities_is_unknown_not_false(self):
        # None 与 False 必须分开：前者是「没告诉我们」，后者是「明确不支持」
        out = mc._from_openai([{"id": "mystery"}])
        self.assertIsNone(out[0]["vision"])

    # ─── Ollama ─────────────────────────────────────

    def test_ollama_reads_name_and_capabilities(self):
        data = {"models": [
            {"name": "has-vision:latest", "capabilities": ["completion", "vision"]},
            {"name": "plain:latest", "capabilities": ["completion", "tools"]},
            {"model": "only-model-field:7b"},
        ]}
        by_id = {m["id"]: m for m in mc._from_ollama(data)}
        self.assertTrue(by_id["has-vision:latest"]["vision"])
        self.assertFalse(by_id["plain:latest"]["vision"])
        self.assertIsNone(by_id["only-model-field:7b"]["vision"])

    def test_ollama_empty_payload(self):
        self.assertEqual(mc._from_ollama({}), [])
        self.assertEqual(mc._from_ollama(None), [])


class ListModelsTest(unittest.TestCase):
    """对外入口：provider 校验、缓存、拉取失败的兜底。"""

    def setUp(self):
        mc.clear_cache()
        self.addCleanup(mc.clear_cache)
        self.fake_providers = {
            "volc": {"label": "火山引擎", "base_url": "http://x", "model": "my-default",
                     "api_key": "k", "vision": False},
        }
        p = mock.patch.object(mc, "PROVIDERS", self.fake_providers)
        p.start()
        self.addCleanup(p.stop)

    def test_unknown_provider(self):
        out = mc.list_models("nope")
        self.assertFalse(out["ok"])
        self.assertEqual(out["models"], [])
        self.assertIn("未知 provider", out["error"])

    def test_empty_provider_id(self):
        self.assertFalse(mc.list_models("")["ok"])
        self.assertFalse(mc.list_models(None)["ok"])

    def test_success_shape_and_cache(self):
        rows = [{"id": "a-260901", "task_type": ["TextGeneration"]}]
        with mock.patch.object(mc, "_fetch", return_value=mc._from_ark(rows)) as f:
            first = mc.list_models("volc")
            self.assertTrue(first["ok"])
            self.assertFalse(first["cached"])
            self.assertEqual(first["models"][0]["id"], "a-260901")

            second = mc.list_models("volc")
            self.assertTrue(second["cached"])
            self.assertEqual(f.call_count, 1)          # 缓存命中就不再打远端

            mc.list_models("volc", force=True)
            self.assertEqual(f.call_count, 2)          # force 绕过缓存

    def test_failure_falls_back_to_configured_default(self):
        # 拉不到时至少留一条，否则下拉空着会被当成「功能坏了」
        with mock.patch.object(mc, "_fetch", side_effect=RuntimeError("boom")):
            out = mc.list_models("volc")
        self.assertFalse(out["ok"])
        self.assertIn("RuntimeError", out["error"])
        self.assertEqual([m["id"] for m in out["models"]], ["my-default"])

    def test_failure_without_default_model_returns_empty(self):
        self.fake_providers["volc"]["model"] = ""
        with mock.patch.object(mc, "_fetch", side_effect=RuntimeError("boom")):
            out = mc.list_models("volc")
        self.assertFalse(out["ok"])
        self.assertEqual(out["models"], [])

    def test_failure_cache_is_dropped_on_force(self):
        with mock.patch.object(mc, "_fetch", side_effect=RuntimeError("boom")):
            self.assertFalse(mc.list_models("volc")["ok"])
        with mock.patch.object(mc, "_fetch",
                               return_value=[{"id": "ok-1", "vision": False,
                                              "retiring": False}]):
            out = mc.list_models("volc", force=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["models"][0]["id"], "ok-1")


class ModelCatalogApiTest(unittest.TestCase):
    """/api/models 的两个分支互不干扰。"""

    def setUp(self):
        self.client = main.app.test_client()

    def test_provider_branch_uses_catalog(self):
        canned = {"ok": True, "cached": False, "error": None,
                  "models": [{"id": "m-1", "vision": False, "retiring": False}]}
        with mock.patch.object(main.model_catalog, "list_models",
                               return_value=canned) as f:
            resp = self.client.get("/api/models?provider=volc")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), canned)
        f.assert_called_once_with("volc")

    def test_empty_provider_falls_through_to_ollama(self):
        # 不带 provider 时必须维持原行为：聊天页仍在用这个分支
        fake = mock.Mock()
        fake.json.return_value = {"models": [{"name": "qwen:9b", "size": 123}]}
        fake.raise_for_status.return_value = None
        with mock.patch.object(main.requests, "get", return_value=fake) as g:
            resp = self.client.get("/api/models?baseUrl=http://127.0.0.1:11434")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        self.assertEqual(resp.get_json()["models"][0]["name"], "qwen:9b")
        self.assertIn("/api/tags", g.call_args[0][0])

    def test_provider_list_never_leaks_key(self):
        canned = {"ok": True, "cached": False, "error": None, "models": []}
        with mock.patch.object(main.model_catalog, "list_models", return_value=canned):
            resp = self.client.get("/api/models?provider=deepseek")
        self.assertNotIn("key", resp.get_data(as_text=True).lower())


if __name__ == "__main__":
    unittest.main()
