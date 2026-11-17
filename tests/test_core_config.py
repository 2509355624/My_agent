"""app.core.config facade 与旧 app.config 的一致性守卫。"""

import unittest

import app.config as legacy
import app.core.config as core


class CoreConfigCompatTest(unittest.TestCase):
    def test_key_paths_match(self):
        for name in (
            "BASE_DIR",
            "ROOT_DIR",
            "WEB_DIR",
            "DATA_DIR",
            "DOCUMENTS_DIR",
            "VECTOR_STORE_DIR",
            "SKILLS_DIR",
            "AGENTS_DIR",
        ):
            self.assertEqual(getattr(core, name), getattr(legacy, name), name)

    def test_state_path_matches(self):
        self.assertEqual(
            core.state_path("compat-check.json"),
            legacy.state_path("compat-check.json"),
        )

    def test_persona_model_helpers_match(self):
        self.assertEqual(core.persona_model_ids(), legacy.persona_model_ids())
        self.assertIsNotNone(core.persona_model_of("deepseek-v4.1-flash"))

    def test_public_names_are_reachable(self):
        for name in ("LLM_PROVIDER", "LLM_FALLBACK_CHAIN", "AGENTS_DIR"):
            self.assertTrue(hasattr(core, name), name)


if __name__ == "__main__":
    unittest.main()
