"""项目骨架守卫：目标包和 README 必须存在。

只检查目录结构，不 import 业务模块；空包阶段也能跑。
"""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

FEATURES = (
    "chat",
    "image",
    "comic",
    "persona",
    "rag",
    "novel",
    "card",
    "qq",
    "admin",
)

REQUIRED_PACKAGES = (
    APP / "core",
    APP / "features",
    APP / "gateways",
    APP / "integrations",
    APP / "persistence",
    APP / "workers",
    APP / "web",
    *[APP / "features" / name for name in FEATURES],
    APP / "integrations" / "comfy",
    APP / "integrations" / "llm",
    APP / "integrations" / "qq",
    APP / "integrations" / "volc",
)

REQUIRED_READMES = (
    APP / "core" / "README.md",
    APP / "features" / "README.md",
    APP / "gateways" / "README.md",
    APP / "integrations" / "README.md",
    APP / "persistence" / "README.md",
    APP / "workers" / "README.md",
    APP / "web" / "README.md",
    *[APP / "features" / name / "README.md" for name in FEATURES],
)


class ProjectSkeletonTest(unittest.TestCase):
    def test_required_packages_exist(self):
        missing = [
            str(path.relative_to(ROOT))
            for path in REQUIRED_PACKAGES
            if not path.is_dir()
        ]
        self.assertEqual([], missing)

    def test_packages_have_init(self):
        missing = [
            str(path.relative_to(ROOT) / "__init__.py")
            for path in REQUIRED_PACKAGES
            if not (path / "__init__.py").is_file()
        ]
        self.assertEqual([], missing)

    def test_required_readmes_exist(self):
        missing = [
            str(path.relative_to(ROOT))
            for path in REQUIRED_READMES
            if not path.is_file()
        ]
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
