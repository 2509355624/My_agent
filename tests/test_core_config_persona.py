"""角色扮演模型配置域：默认模型与玩家白名单。"""

import unittest

from app import config
from app.core.config import llm, persona


class PersonaConfigTest(unittest.TestCase):
    def test_every_exported_name_matches_legacy_config(self):
        for name in persona.__all__:
            self.assertTrue(hasattr(config, name), name)
            self.assertEqual(getattr(config, name), getattr(persona, name), name)

    def test_model_whitelist_helpers(self):
        ids = persona.persona_model_ids()
        first = persona.PERSONA_MODELS[0]["id"]
        self.assertIn(first, ids)
        self.assertEqual(persona.persona_model_of(first)["id"], first)
        self.assertIsNone(persona.persona_model_of("not-in-whitelist"))

    def test_llm_still_reexports_persona_defaults(self):
        self.assertEqual(llm.PERSONA_MODEL, persona.PERSONA_MODEL)
        self.assertEqual(llm.PERSONA_MODEL_DEFAULT, persona.PERSONA_MODEL_DEFAULT)
        self.assertEqual(llm.PERSONA_MODELS, persona.PERSONA_MODELS)
        self.assertIn("persona_model_of", llm.__all__)
        self.assertFalse([name for name in persona.__all__ if name.startswith("_")])


if __name__ == "__main__":
    unittest.main()
