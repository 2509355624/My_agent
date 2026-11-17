"""识图配置域：旧 app.config 兼容层与新模块同值，审核阈值归 image。"""

import unittest

from app import config
from app.core.config import image, vision


class VisionConfigTest(unittest.TestCase):
    def test_old_and_new_paths_expose_the_same_values(self):
        for name in (
            "VISION_PROVIDER",
            "VISION_MODEL",
            "OLLAMA_VISION_NUM_GPU",
            "OLLAMA_VISION_KEEP_ALIVE",
            "VISION_TIMEOUT",
            "VISION_MAX_EDGE",
            "VISION_PROMPT_MAX",
        ):
            self.assertTrue(hasattr(vision, name), name)
            self.assertEqual(getattr(config, name), getattr(vision, name), name)

    def test_audit_thresholds_live_with_image_not_vision(self):
        for name in ("IMAGE_AUDIT_TIMEOUT", "IMAGE_AUDIT_PROMPT_MAX"):
            self.assertTrue(hasattr(image, name), name)
            self.assertEqual(getattr(config, name), getattr(image, name), name)
            self.assertFalse(hasattr(vision, name), name)

    def test_public_names_are_exported(self):
        self.assertIn("VISION_PROVIDER", vision.__all__)
        self.assertIn("VISION_PROMPT_MAX", vision.__all__)
        self.assertNotIn("os", vision.__all__)


if __name__ == "__main__":
    unittest.main()
