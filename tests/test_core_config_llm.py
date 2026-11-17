"""LLM 运行期设置：默认值、校验、热更新。"""

import os
import tempfile
import unittest

from app import llm as app_llm
from app.core.config import llm, runtime


class LlmRuntimeSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_RUNTIME_SETTINGS")
        os.environ["AGENT_RUNTIME_SETTINGS"] = os.path.join(
            self.tmp.name, "runtime_settings.json")
        runtime.clear_cache()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_RUNTIME_SETTINGS", None)
        else:
            os.environ["AGENT_RUNTIME_SETTINGS"] = self.old
        runtime.clear_cache()
        self.tmp.cleanup()

    def test_defaults_match_domain_constants(self):
        s = llm.get_llm_settings()
        self.assertEqual(s["fallback_chain"], llm.LLM_FALLBACK_CHAIN)
        self.assertEqual(s["request_timeout"], llm.LLM_REQUEST_TIMEOUT)
        self.assertEqual(s["retry_times"], llm.LLM_RETRY_TIMES)
        self.assertEqual(s["retry_budget"], llm.LLM_RETRY_BUDGET)

    def test_update_is_hot_and_lands_on_disk(self):
        effective, err = llm.update_llm_settings({
            "LLM_FALLBACK_CHAIN": "volc:vm,scnet2:sm",
            "LLM_REQUEST_TIMEOUT": 30,
            "LLM_RETRY_TIMES": 2,
        })
        self.assertIsNone(err)
        self.assertEqual(effective["fallback_chain"], "volc:vm,scnet2:sm")
        self.assertEqual(effective["request_timeout"], 30)
        self.assertEqual(effective["retry_times"], 2)
        self.assertTrue(os.path.exists(runtime.runtime_path()))
        # app/llm 下一次请求读到的是同一份新值，不需要重启。
        self.assertEqual(app_llm._settings()["fallback_chain"],
                         "volc:vm,scnet2:sm")

    def test_provider_hot_update_reaches_request_default(self):
        effective, err = llm.update_llm_settings({"LLM_PROVIDER": "deepseek"})
        self.assertIsNone(err)
        self.assertEqual(app_llm.default_provider(), "deepseek")
        self.assertEqual(app_llm.get_effective_config()["provider"], "deepseek")

    def test_unknown_provider_is_rejected(self):
        effective, err = llm.update_llm_settings({
            "LLM_FALLBACK_CHAIN": "nope:x,volc:vm",
        })
        self.assertIsNone(effective)
        self.assertIn("未知 provider", err)

    def test_numeric_range_is_enforced(self):
        effective, err = llm.update_llm_settings({"LLM_REQUEST_TIMEOUT": 1})
        self.assertIsNone(effective)
        self.assertIn("5", err)

    def test_none_removes_override(self):
        llm.update_llm_settings({"LLM_RETRY_TIMES": 2})
        effective, err = llm.update_llm_settings({"LLM_RETRY_TIMES": None})
        self.assertIsNone(err)
        self.assertEqual(effective["retry_times"], llm.LLM_RETRY_TIMES)


if __name__ == "__main__":
    unittest.main()
