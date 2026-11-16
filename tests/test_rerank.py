"""本地 cross-encoder 重排模块的测试。

模型 1GB 不进测试：只测降级路径 + 开关 + 打分排序逻辑（用假模型），
真模型加载留给手工验证（tools/_verify_rerank.py）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import rerank


class RerankTest(unittest.TestCase):
    def setUp(self):
        # 每个用例重置模块级单例，避免互相污染
        self._model = rerank._MODEL
        self._failed = rerank._FAILED
        self._ms = rerank._LOAD_MS
        rerank._MODEL = None
        rerank._FAILED = False
        rerank._LOAD_MS = 0
        self._env = os.environ.get("RAG_RERANK_ENABLED")
        os.environ["RAG_RERANK_ENABLED"] = "1"

    def tearDown(self):
        rerank._MODEL = self._model
        rerank._FAILED = self._failed
        rerank._LOAD_MS = self._ms
        if self._env is None:
            os.environ.pop("RAG_RERANK_ENABLED", None)
        else:
            os.environ["RAG_RERANK_ENABLED"] = self._env

    def test_missing_model_degrades_to_original_order(self):
        """模型目录不存在 → 原样返回，不抛异常（fail-open）。"""
        with mock.patch.object(rerank, "_MODEL_DIR", "/nonexistent/rerank-model"):
            docs = [{"content": "a"}, {"content": "b"}]
            out = rerank.rerank("q", docs)
            self.assertEqual([d["content"] for d in out], ["a", "b"])
            self.assertTrue(rerank._FAILED)

    def test_disabled_returns_original_order(self):
        os.environ["RAG_RERANK_ENABLED"] = "0"
        self.assertFalse(rerank.enabled())
        docs = [{"content": "a"}, {"content": "b"}]
        out = rerank.rerank("q", docs)
        self.assertEqual([d["content"] for d in out], ["a", "b"])

    def test_rerank_sorts_by_score_and_keeps_metadata(self):
        """假模型按分数重排，distance/metadata 等原样保留。"""
        fake = mock.Mock()
        fake.predict.return_value = [0.1, 0.9, 0.5]
        rerank._MODEL = fake
        docs = [
            {"content": "低分", "distance": 0.2, "metadata": {"kind": "fact"}},
            {"content": "高分", "distance": 0.4, "metadata": {"kind": "raw"}},
            {"content": "中分", "distance": 0.1},
        ]
        out = rerank.rerank("问题", docs)
        self.assertEqual([d["content"] for d in out], ["高分", "中分", "低分"])
        self.assertEqual(out[0]["distance"], 0.4)
        self.assertEqual(out[0]["metadata"], {"kind": "raw"})
        # 传给模型的 pairs 必须是 [query, text] 结构
        pairs = fake.predict.call_args.args[0]
        self.assertEqual(pairs[0][0], "问题")
        self.assertEqual(pairs[0][1], "低分")

    def test_top_n_truncates(self):
        fake = mock.Mock()
        fake.predict.return_value = [0.1, 0.9, 0.5]
        rerank._MODEL = fake
        docs = [{"content": "a"}, {"content": "b"}, {"content": "c"}]
        out = rerank.rerank("q", docs, top_n=2)
        self.assertEqual([d["content"] for d in out], ["b", "c"])

    def test_empty_and_bad_docs(self):
        self.assertEqual(rerank.rerank("q", []), [])
        fake = mock.Mock()
        fake.predict.return_value = [0.9]
        rerank._MODEL = fake
        docs = [{"content": "ok"}, {"metadata": {}}, "not-a-dict"]
        out = rerank.rerank("q", docs)
        self.assertEqual(out[0]["content"], "ok")
        self.assertEqual(len(out), 3)

    def test_score_failure_degrades(self):
        fake = mock.Mock()
        fake.predict.side_effect = RuntimeError("boom")
        rerank._MODEL = fake
        docs = [{"content": "a"}, {"content": "b"}]
        out = rerank.rerank("q", docs)
        self.assertEqual([d["content"] for d in out], ["a", "b"])

    def test_status_snapshot(self):
        s = rerank.status()
        self.assertIn("enabled", s)
        self.assertIn("model_ready", s)
        self.assertIn("model_dir", s)


if __name__ == "__main__":
    unittest.main()
