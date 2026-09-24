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
import app.tools.normal.list_skills as list_skills_tool


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


class SkillSummaryTest(unittest.TestCase):
    """一行简介的抽取规则。

    带 YAML frontmatter 的规范首行是 `---`，直接取首行会得到一个对选 skill
    毫无信息量的 `---`（system prompt 与 list_skills 都会踩）。
    """

    def test_yaml_frontmatter_is_skipped(self):
        md = "---\nname: writing\ndescription: 很长的一段\n---\n\n# 中文写作总装 Skill\n\n正文"
        self.assertEqual(skills.skill_summary(md), "中文写作总装 Skill")

    def test_frontmatter_terminated_by_dots(self):
        self.assertEqual(skills.skill_summary("---\nname: x\n...\n# 标题\n"), "标题")

    def test_plain_heading_without_frontmatter(self):
        self.assertEqual(
            skills.skill_summary("# 生图 Skill v2（涩图动作优化版）\n\n正文"),
            "生图 Skill v2（涩图动作优化版）")

    def test_multilevel_heading_is_stripped(self):
        self.assertEqual(skills.skill_summary("## 二级标题\n"), "二级标题")

    def test_fallback_to_first_body_line(self):
        md = "---\nname: x\n---\n\n正文第一行\n第二行"
        self.assertEqual(skills.skill_summary(md), "正文第一行")

    def test_fallback_skips_separator_line(self):
        md = "---\nname: x\n---\n\n----\n\n正文"
        self.assertEqual(skills.skill_summary(md), "正文")

    def test_unclosed_frontmatter_yields_empty(self):
        self.assertEqual(skills.skill_summary("---\nname: x\ndescription: y"), "")

    def test_empty_input(self):
        self.assertEqual(skills.skill_summary(""), "")


class ListSkillsToolTest(unittest.TestCase):
    """list_skills 工具的展示层（不能出现 `---` 这种无信息量的简介）"""

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

    def test_frontmatter_skill_shows_heading_not_dashes(self):
        self._write("fm/skill.md", "---\nname: fm\ndescription: 说明\n---\n\n# 有标题的规范\n")
        out = list_skills_tool._list_skills()
        self.assertIn("- **fm**: 有标题的规范", out)
        self.assertNotIn("---", out)

    def test_skill_without_spec_shows_placeholder(self):
        os.makedirs(os.path.join(self.root, "bare"), exist_ok=True)
        self.assertIn("- **bare**: (无说明)", list_skills_tool._list_skills())

    def test_empty_skills_dir(self):
        self.assertEqual(list_skills_tool._list_skills(), "暂无可用 Skill")


class RealSkillsDataTest(unittest.TestCase):
    """真实 skills/ 里每个 workflow.json 都必须能被 load_skill 解析。

    这是「数据契约」测试。模板里的 `__SEED__` 是**故意不加引号**的非法 JSON，
    靠 load_skill 先做 `": __SEED__" -> ': "__SEED__"'` 的字面替换再 parse。
    哪天写成 `"seed":__SEED__`（少个空格）或换了字段风格，替换就会落空，
    parse 直接失败、load_skill 返回 None——而症状只在真机生不出图时才暴露。

    skills/ 是本机私有数据（已 gitignore），换机或 CI 上不存在则跳过。
    """

    def test_every_workflow_parses(self):
        skills_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
        if not os.path.isdir(skills_dir):
            self.skipTest("本机没有 skills/ 数据目录")

        checked = []
        for name in sorted(os.listdir(skills_dir)):
            if not os.path.isfile(os.path.join(skills_dir, name, "workflow.json")):
                continue
            data = skills.load_skill(name)
            self.assertIsNotNone(
                data, "skill '%s' 的 workflow.json 解析失败（检查 __SEED__ 占位写法）" % name)
            self.assertIsNotNone(data["workflow"], "skill '%s' 解析出空工作流" % name)
            for nid, node in data["workflow"].items():
                self.assertIn("class_type", node,
                              "skill '%s' 节点 %s 缺 class_type" % (name, nid))
            checked.append(name)

        self.assertTrue(checked, "skills/ 下没找到任何 workflow.json，这个测试形同虚设")


if __name__ == "__main__":
    unittest.main()
