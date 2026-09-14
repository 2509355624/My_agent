# -*- coding: utf-8 -*-
"""RAG 向量库工具函数测试（app/rag/vector_store.py）。

只测纯函数 safe_collection_name：ChromaDB 对 collection 名有硬约束
（3-63 字符、必须字母开头），用户又可能传任意中文知识库名，所以这里
的净化规则值得单独钉死。真正的检索需要 ChromaDB + 向量模型，
若环境未安装相关依赖则整体跳过。
"""

import unittest

try:
    from app.rag.vector_store import safe_collection_name
    _IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - 取决于环境是否装了 chromadb
    safe_collection_name = None
    _IMPORT_ERROR = str(e)


@unittest.skipIf(safe_collection_name is None,
                 "未安装 chromadb，跳过 RAG 向量库测试：%s" % _IMPORT_ERROR)
class SafeCollectionNameTest(unittest.TestCase):
    def test_lowercased_and_sanitized(self):
        self.assertEqual(safe_collection_name("My KB!"), "my_kb_")

    def test_leading_digit_gets_prefix(self):
        self.assertEqual(safe_collection_name("123"), "kb_123")

    def test_empty_falls_back_to_default(self):
        self.assertEqual(safe_collection_name(""), "default")

    def test_chinese_name_is_sanitized(self):
        name = safe_collection_name("面试知识")
        self.assertTrue(name.startswith("kb_"))
        self.assertRegex(name, r"^[a-z_][a-z0-9_-]*$")

    def test_long_name_truncated_under_63_chars(self):
        name = safe_collection_name("a" * 100)
        self.assertLessEqual(len(name), 63)
        self.assertTrue(name.startswith("a" * 54 + "_"))

    def test_result_matches_chroma_constraints_for_realistic_names(self):
        for raw in ["kb-1", "9lives", "  spaces  ", "混English中文", "documents"]:
            name = safe_collection_name(raw)
            self.assertGreaterEqual(len(name), 3)
            self.assertLessEqual(len(name), 63)
            self.assertTrue(name[0].isalpha())

    def test_very_short_name_is_not_padded(self):
        # 记录现状：净化结果短于 3 字符时不会补足下限。ChromaDB 要求
        # collection 名 3-63 字符，所以像 "x"/"ab" 这类极短知识库名会在
        # 之后 get_or_create_collection 时被 ChromaDB 拒绝。
        # 属既有行为，本测试只如实记录，不做修复。
        self.assertEqual(safe_collection_name("x"), "x")


if __name__ == "__main__":
    unittest.main()
