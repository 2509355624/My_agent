# -*- coding: utf-8 -*-
"""Skill 目录解析与加载测试（app/skills.py）。

Skill 是用户的"个人知识"载体，目录结构有两种风格（本地生图 skill 用
小写 skill.md + workflow.json；GitHub 下载的脚手架用大写 SKILL.md +
VERSION + references/），还可能多一层同名嵌套。这些兼容规则值得钉住。
"""

import os
import tempfile
import unittest
from unittest import mock

import app.skills as skills


class SkillLoaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        p = mock.patch.object(skills, "SKILLS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, rel, content):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_list_skills_returns_directories_only(self):
        os.makedirs(os.path.join(self.root, "alpha"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "beta"), exist_ok=True)
        self._write("loose.txt", "顶层文件不算 skill")
        self.assertEqual(sorted(skills.list_skills()), ["alpha", "beta"])

    def test_load_lowercase_skill_md(self):
        self._write("plain/skill.md", "小写规范")
        data = skills.load_skill("plain")
        self.assertEqual(data["skill_md"], "小写规范")
        self.assertIsNone(data["workflow"])
        self.assertEqual(data["version"], "")

    def test_uppercase_skill_md_takes_priority(self):
        self._write("up/skill.md", "小写")
        self._write("up/SKILL.md", "大写优先")
        self.assertEqual(skills.load_skill("up")["skill_md"], "大写优先")

    def test_nested_same_name_dir_is_resolved(self):
        # GitHub 风格：skills/x/x/SKILL.md
        self._write("nested/nested/SKILL.md", "嵌套规范")
        data = skills.load_skill("nested")
        self.assertEqual(data["skill_md"], "嵌套规范")
        self.assertTrue(data["path"].endswith(os.path.join("nested", "nested")))

    def test_version_and_references_are_collected(self):
        self._write("doc/SKILL.md", "主规范")
        self._write("doc/VERSION", "1.2.3")
        self._write("doc/references/a.md", "参考A")
        self._write("doc/references/b.md", "参考B")
        data = skills.load_skill("doc")
        self.assertEqual(data["version"], "1.2.3")
        self.assertIn("<!-- 参考: a.md -->", data["references"])
        self.assertIn("参考A", data["references"])
        self.assertIn("参考B", data["references"])

    def test_character_file_is_stripped(self):
        self._write("img/skill.md", "生图")
        self._write("img/character.txt", "  角色底模  ")
        self.assertEqual(skills.load_skill("img")["character"], "角色底模")

    def test_workflow_seed_placeholder_is_repaired(self):
        # workflow.json 里种子常写成裸的 __SEED__（非法 JSON），加载时应修好
        self._write("img2/skill.md", "生图")
        self._write("img2/workflow.json",
                    '{"3": {"inputs": {"seed": __SEED__}, "class_type": "KSampler"}}')
        data = skills.load_skill("img2")
        self.assertIsNotNone(data["workflow"])
        self.assertEqual(data["workflow"]["3"]["inputs"]["seed"], "__SEED__")

    def test_broken_workflow_returns_none(self):
        self._write("bad/skill.md", "坏")
        self._write("bad/workflow.json", "{ 这不是 JSON")
        with mock.patch("builtins.print"):  # 屏蔽 loader 的警告输出
            self.assertIsNone(skills.load_skill("bad"))

    def test_missing_skill_returns_none(self):
        self.assertIsNone(skills.load_skill("ghost"))

    def test_skill_without_spec_returns_empty_md(self):
        os.makedirs(os.path.join(self.root, "empty"), exist_ok=True)
        data = skills.load_skill("empty")
        self.assertEqual(data["skill_md"], "")
        self.assertEqual(data["references"], "")


if __name__ == "__main__":
    unittest.main()
