"""生图配置域：旧 app.config 兼容层与新模块同值。"""

import unittest

from app import config
from app.core.config import image


class ImageConfigTest(unittest.TestCase):
    def test_old_and_new_paths_expose_the_same_values(self):
        for name in (
            "ENABLE_IMAGE_GEN",
            "COMFYUI_URL",
            "COMFYUI_ROOT",
            "COMFYUI_OUTPUT_DIR",
            "IMAGE_DEFAULT_SKILL",
            "IMAGE_GEN_TIMEOUT",
            "COMFY_MIN_FREE_RAM_GB",
            "COMFY_MIN_FREE_VRAM_GB",
            "COMFY_RELEASE_AFTER_SAME",
            "COMFY_RELEASE_AFTER_TOTAL",
            "COMFY_RELEASE_AFTER_COMIC",
            "QWEN_COOLDOWN",
            "DISABLED_IMAGE_SKILLS",
            "NAI_ENABLED",
            "NAI_OPEN_ACCESS",
        ):
            self.assertTrue(hasattr(image, name), name)
            self.assertEqual(getattr(config, name), getattr(image, name), name)

    def test_channel_map_keeps_persona_fast_default(self):
        self.assertEqual(config.COMFY_RELEASE_AFTER_SAME_MAP.get("_persona_fast"), 8)
        self.assertEqual(config.COMFY_RELEASE_AFTER_SAME_MAP,
                         image.COMFY_RELEASE_AFTER_SAME_MAP)

    def test_public_names_are_exported(self):
        self.assertIn("COMFYUI_URL", image.__all__)
        self.assertIn("NAI_API_KEY", image.__all__)
        self.assertNotIn("os", image.__all__)


if __name__ == "__main__":
    unittest.main()
