# -*- coding: utf-8 -*-
"""documents 工具测试：分段读取 / 搜索 / 路径沙箱（app/tools/normal/documents.py）。

路径沙箱是安全相关逻辑（LLM 可以自由传 filename），单独钉死：
任何输入都必须落回 documents 目录内。
"""

import os
import tempfile
import unittest
from unittest import mock

import app.tools.normal.documents as docs


class DocumentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        p = mock.patch.object(docs, "DOCUMENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, name, lines):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _inside(self, path):
        return os.path.realpath(path).startswith(os.path.realpath(self.root))

    # ─── 路径沙箱 ────────────────────────────────────

    def test_safe_path_rejects_empty(self):
        target, err = docs._safe_path("")
        self.assertIsNone(target)
        self.assertIsNotNone(err)

    def test_safe_path_keeps_relative_path_inside(self):
        target, err = docs._safe_path("sub/a.txt")
        self.assertIsNone(err)
        self.assertTrue(self._inside(target))

    def test_safe_path_neutralizes_dotdot(self):
        target, err = docs._safe_path("../../../secret.txt")
        self.assertIsNone(err)
        self.assertTrue(self._inside(target))

    # ─── 读取 ────────────────────────────────────────

    def test_read_document_has_line_numbers(self):
        self._write("a.txt", ["第一行", "第二行", "第三行"])
        out = docs.read_document("a.txt")
        self.assertIn("1\t第一行", out)
        self.assertIn("3\t第三行", out)
        self.assertIn("共 3 行", out)

    def test_read_document_offset_and_limit(self):
        self._write("big.txt", ["line-%d" % i for i in range(1, 11)])
        out = docs.read_document("big.txt", offset=3, limit=2)
        self.assertIn("3\tline-3", out)
        self.assertIn("4\tline-4", out)
        self.assertNotIn("5\tline-5", out)
        self.assertIn("第 3-4 行", out)

    def test_read_document_limit_is_capped_at_500(self):
        self._write("huge.txt", ["x%d" % i for i in range(600)])
        out = docs.read_document("huge.txt", limit=9999)
        self.assertIn("共 600 行", out)
        numbered = [ln for ln in out.split("\n") if "\t" in ln]
        self.assertEqual(len(numbered), 500)
        self.assertIn("还有 100 行未显示", out)

    def test_read_missing_file(self):
        self.assertIn("不存在", docs.read_document("nope.txt"))

    # ─── 搜索 ────────────────────────────────────────

    def test_search_document_finds_with_context(self):
        self._write("s.txt", ["apple", "banana", "cherry", "apple pie"])
        out = docs.search_document("s.txt", "apple")
        self.assertIn("找到 2 处", out)
        self.assertIn("apple pie", out)

    def test_search_document_is_case_insensitive(self):
        self._write("c.txt", ["Hello World"])
        self.assertIn("找到 1 处", docs.search_document("c.txt", "hello"))

    def test_search_document_caps_matches_at_10(self):
        self._write("many.txt", ["hit %d" % i for i in range(15)])
        self.assertIn("找到 10 处", docs.search_document("many.txt", "hit"))

    def test_search_document_no_match(self):
        self._write("n.txt", ["a"])
        self.assertIn("未找到", docs.search_document("n.txt", "zzz"))

    # ─── 信息 / 列表 ─────────────────────────────────

    def test_file_info(self):
        self._write("i.txt", ["1", "2", "3"])
        out = docs.file_info("i.txt")
        self.assertIn("i.txt", out)
        self.assertIn("行数: 3", out)

    def test_file_info_missing_file(self):
        self.assertIn("不存在", docs.file_info("ghost.txt"))

    def test_list_documents_empty_then_nonempty(self):
        self.assertIn("为空", docs.list_documents())
        self._write("x.txt", ["a"])
        self.assertIn("x.txt", docs.list_documents())


if __name__ == "__main__":
    unittest.main()
