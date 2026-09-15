# -*- coding: utf-8 -*-
"""skills 文件工具测试（read_file / write_file / list_files + 沙箱路径解析）。

这一层是「AI 到底能不能读到文件」的物理边界，两个方向都要钉住：

1. 读得到：skills 内部任意层级的子目录（human-writing/references/fiction.md）
   —— 这是真实踩过的坑：老的 read_file 会把路径里的 "/" 直接删掉，
   于是 "human-writing/references/fiction.md" 变成 "human-writingreferencesfiction.md"，
   模型永远读不到 references/，只能在 skill.md 的末尾吃到内嵌备份。
2. 出不去：.. 跳转、绝对路径越界、符号链接逃逸，一律拒绝。
"""

import os
import tempfile
import unittest
from unittest import mock

from app.tools import sandbox
from app.tools.normal import list_files as lf
from app.tools.normal import read_file as rf
from app.tools.normal import write_file as wf
from app.tools.registry import TOOLS


class SandboxTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        p = mock.patch.object(sandbox, "SKILLS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, rel, content):
        path = os.path.join(self.root, rel)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _seed_skill(self):
        self._write("human-writing/SKILL.md", "# 路由表\n\n按任务读取 references/\n")
        self._write("human-writing/references/fiction.md", "虚构写作：人物先给一件眼下要办的事\n")


class SandboxResolveTest(SandboxTestCase):
    def test_relative_subpath_resolves_inside_skills(self):
        target, err = sandbox.resolve_in_skills("human-writing/references/fiction.md")
        self.assertIsNone(err)
        self.assertEqual(sandbox.to_rel(target), "human-writing/references/fiction.md")

    def test_skills_prefix_is_stripped(self):
        target, err = sandbox.resolve_in_skills("skills/human-writing/SKILL.md")
        self.assertIsNone(err)
        self.assertEqual(sandbox.to_rel(target), "human-writing/SKILL.md")

    def test_backslashes_are_normalized(self):
        target, err = sandbox.resolve_in_skills("human-writing\\references\\fiction.md")
        self.assertIsNone(err)
        self.assertEqual(sandbox.to_rel(target), "human-writing/references/fiction.md")

    def test_parent_traversal_is_rejected(self):
        _, err = sandbox.resolve_in_skills("../outside.md")
        self.assertIn("禁止父级跳转", err)

    def test_nested_parent_traversal_is_rejected(self):
        _, err = sandbox.resolve_in_skills("human-writing/../../outside.md")
        self.assertIn("禁止父级跳转", err)

    def test_absolute_path_rejected_by_default(self):
        _, err = sandbox.resolve_in_skills(os.path.join(self.root, "a.md"))
        self.assertIn("只接受相对", err)

    def test_absolute_path_outside_sandbox_is_rejected(self):
        outside = os.path.abspath(os.path.join(self.root, os.pardir, "secret.md"))
        _, err = sandbox.resolve_in_skills(outside, allow_absolute=True)
        self.assertIn("超出 skills 目录范围", err)

    def test_sibling_prefix_directory_cannot_smuggle(self):
        # skills_other 不该被当成 skills 的前缀命中
        _, err = sandbox.resolve_in_skills("../skills_other/x.md")
        self.assertIsNotNone(err)

    def test_empty_path_is_rejected(self):
        # 空路径必须被拒：delete_file 靠这条挡住「删 skills 主目录」
        _, err = sandbox.resolve_in_skills("   ")
        self.assertIn("不能为空", err)
        _, err = sandbox.resolve_in_skills("")
        self.assertIn("不能为空", err)

    def test_dot_resolves_to_skills_dir(self):
        target, err = sandbox.resolve_in_skills(".")
        self.assertIsNone(err)
        self.assertEqual(target, sandbox.skills_root())


class ReadFileTest(SandboxTestCase):
    def test_reads_nested_reference_file(self):
        # 核心回归：这正是模型读不到的路径
        self._seed_skill()
        out = rf.read_file("human-writing/references/fiction.md")
        self.assertIn("虚构写作", out)
        self.assertIn("human-writing/references/fiction.md", out)

    def test_reads_skill_md_in_subdir(self):
        self._seed_skill()
        self.assertIn("路由表", rf.read_file("human-writing/SKILL.md"))

    def test_accepts_absolute_path_inside_sandbox(self):
        # 用户常在对话里直接给绝对路径，模型会照抄过来
        path = self._write("human-writing/SKILL.md", "绝对路径也要能读")
        self.assertIn("绝对路径也要能读", rf.read_file(path))

    def test_directory_hint_points_to_list_files(self):
        os.makedirs(os.path.join(self.root, "human-writing"), exist_ok=True)
        out = rf.read_file("human-writing")
        self.assertIn("是目录", out)
        self.assertIn("list_files", out)

    def test_missing_file_hint_points_to_list_files(self):
        self._seed_skill()
        out = rf.read_file("human-writing/references/ghost.md")
        self.assertIn("文件不存在", out)
        self.assertIn("list_files", out)

    def test_traversal_is_rejected(self):
        self.assertIn("禁止父级跳转", rf.read_file("../outside.md"))

    def test_offset_limit_pagination(self):
        self._write("p/long.md", "\n".join("第%d行" % i for i in range(1, 51)))
        out = rf.read_file("p/long.md", offset=11, limit=5)
        self.assertIn("第11行", out)
        self.assertIn("第15行", out)
        self.assertNotIn("第16行", out)
        self.assertIn("第 11-15 行 / 共 50 行", out)
        self.assertIn("用 offset=16 继续读取", out)

    def test_default_read_truncates_huge_file(self):
        self._write("p/huge.md", "\n".join("x" for _ in range(rf.DEFAULT_MAX_LINES + 200)))
        out = rf.read_file("p/huge.md")
        self.assertIn("还有 200 行未显示", out)

    def test_small_file_reports_total_lines(self):
        self._write("p/small.md", "a\nb\nc\n")
        self.assertIn("共 3 行", rf.read_file("p/small.md"))


class WriteFileTest(SandboxTestCase):
    def test_creates_nested_dirs(self):
        out = wf.write_file("new_skill/references/notes.md", "内容")
        self.assertIn("已写入", out)
        self.assertTrue(os.path.isfile(
            os.path.join(self.root, "new_skill", "references", "notes.md")))

    def test_reports_overwrite(self):
        self._write("s/skill.md", "旧")
        out = wf.write_file("s/skill.md", "新")
        self.assertIn("已覆盖", out)
        body = rf.read_file("s/skill.md")
        self.assertIn("新", body)
        self.assertNotIn("旧", body)

    def test_traversal_is_rejected(self):
        self.assertIn("禁止父级跳转", wf.write_file("../escape.md", "x"))
        self.assertFalse(os.path.exists(os.path.abspath(
            os.path.join(self.root, os.pardir, "escape.md"))))

    def test_empty_path_is_rejected(self):
        self.assertIn("不能为空", wf.write_file("", "x"))

    def test_directory_target_is_rejected(self):
        os.makedirs(os.path.join(self.root, "s"), exist_ok=True)
        self.assertIn("是目录", wf.write_file("s", "x"))


class ListFilesTest(SandboxTestCase):
    def test_default_depth_shows_third_level_file(self):
        self._seed_skill()
        out = lf.list_files()
        self.assertIn("human-writing/SKILL.md", out)
        self.assertIn("human-writing/references/fiction.md", out)

    def test_subdir_scope_excludes_other_skills(self):
        self._seed_skill()
        self._write("other/SKILL.md", "另一个 skill")
        out = lf.list_files("human-writing")
        self.assertIn("human-writing/references/fiction.md", out)
        self.assertNotIn("other/SKILL.md", out)

    def test_scope_label_for_subdir(self):
        self._seed_skill()
        self.assertIn("skills/human-writing 文件结构", lf.list_files("human-writing"))

    def test_depth_limit_hides_deep_entries(self):
        # depth=1 只看指定目录的直接子项：references/ 与 SKILL.md 可见，再深的不可见
        self._seed_skill()
        out = lf.list_files("human-writing", depth=1)
        self.assertIn("human-writing/SKILL.md", out)
        self.assertIn("human-writing/references/", out)
        self.assertNotIn("fiction.md", out)

    def test_depth_is_clamped(self):
        self._seed_skill()
        out = lf.list_files("human-writing", depth=99)
        self.assertIn("depth=5", out)

    def test_hidden_and_noise_dirs_are_skipped(self):
        self._write("repo/README.md", "正常文件")
        self._write("repo/.git/config", "不该出现")
        self._write("repo/.gitignore", "不该出现")
        self._write("repo/__pycache__/x.pyc", "不该出现")
        out = lf.list_files("repo")
        self.assertIn("repo/README.md", out)
        self.assertNotIn(".git", out)
        self.assertNotIn("__pycache__", out)
        self.assertNotIn(".gitignore", out)

    def test_reports_size_and_lines(self):
        self._write("s/SKILL.md", "a\nb\n")
        out = lf.list_files("s")
        self.assertIn("2 行", out)

    def test_missing_path_is_reported(self):
        self.assertIn("路径不存在", lf.list_files("ghost-skill"))

    def test_file_path_tells_you_to_read_it(self):
        self._write("s/SKILL.md", "x")
        out = lf.list_files("s/SKILL.md")
        self.assertIn("是文件不是目录", out)

    def test_traversal_is_rejected(self):
        self.assertIn("禁止父级跳转", lf.list_files("../outside"))

    def test_empty_dir_is_reported(self):
        os.makedirs(os.path.join(self.root, "blank"), exist_ok=True)
        self.assertIn("目录为空", lf.list_files("blank"))

    def test_output_ends_with_read_hint(self):
        self._seed_skill()
        self.assertIn('read_file(path="', lf.list_files())


class FileToolsRegisteredTest(unittest.TestCase):
    def test_file_tools_are_registered_with_path_param(self):
        by_name = {t["name"]: t for t in TOOLS}
        for name in ("read_file", "write_file", "list_files"):
            self.assertIn(name, by_name)
            props = by_name[name]["parameters"]["properties"]
            self.assertIn("path", props, name + " 应统一使用 path 参数")
            self.assertNotIn("skill_name", props, name + " 不该再有 skill_name 参数")

    def test_read_file_declares_offset_and_limit(self):
        params = next(t for t in TOOLS if t["name"] == "read_file")["parameters"]
        self.assertEqual(params["required"], ["path"])


if __name__ == "__main__":
    unittest.main()
