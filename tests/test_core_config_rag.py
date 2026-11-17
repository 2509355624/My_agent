"""RAG 配置域：开关函数、角色记忆、世界书预算仍可从旧路径取到。"""

import os
import unittest
from unittest import mock

from app import config
from app.core.config import rag


class RagConfigTest(unittest.TestCase):
    def test_old_and_new_paths_expose_the_same_values(self):
        for name in (
            "PERSONA_RAG",
            "PERSONA_RAG_MODEL",
            "PERSONA_RAG_DATA_DIR",
            "PERSONA_RAG_K",
            "PERSONA_RAG_MAX_DISTANCE",
            "PERSONA_RAG_TIMEOUT",
            "PERSONA_RAG_INGEST_EVERY",
            "PERSONA_LORE_TOKENS",
            "PERSONA_LORE_RAG",
        ):
            self.assertTrue(hasattr(rag, name), name)
            self.assertEqual(getattr(config, name), getattr(rag, name), name)

    def test_function_is_exported_and_still_reads_env(self):
        self.assertIs(config.rag_enabled, rag.rag_enabled)
        with mock.patch.dict(os.environ, {"ENABLE_RAG": "0"}):
            self.assertFalse(config.rag_enabled())
        with mock.patch.dict(os.environ, {"ENABLE_RAG": "1"}):
            self.assertTrue(config.rag_enabled())

    def test_role_memory_store_stays_separate(self):
        self.assertIn("chroma_persona", rag.PERSONA_RAG_DATA_DIR)
        self.assertEqual(rag.PERSONA_RAG_DATA_DIR, config.PERSONA_RAG_DATA_DIR)

    def test_public_names_are_exported(self):
        self.assertIn("rag_enabled", rag.__all__)
        self.assertIn("PERSONA_RAG_K", rag.__all__)
        self.assertNotIn("BASE_DIR", rag.__all__)
        self.assertNotIn("os", rag.__all__)


if __name__ == "__main__":
    unittest.main()
