# -*- coding: utf-8 -*-
"""工具注册表与 RAG 工具层测试。

- registry：注册/执行/未知工具/工具抛异常时的兜底
- kb_tools：切块逻辑（纯函数）+ 各种返回值的文案格式（用假 client，不碰 ChromaDB）
"""

import json
import re
import unittest
from unittest import mock

from app.tools.registry import TOOLS, register_tool, execute_tool
from app.tools.rag import kb_tools


class RegistryTest(unittest.TestCase):
    def test_every_tool_is_well_formed(self):
        self.assertTrue(TOOLS, "注册表不应为空")
        names = [t["name"] for t in TOOLS]
        self.assertEqual(len(names), len(set(names)), "工具名不能重复")
        for t in TOOLS:
            self.assertTrue(t["name"])
            self.assertTrue(t["description"])
            self.assertTrue(callable(t["function"]))
            self.assertIsInstance(t["parameters"], dict)

    def test_execute_known_tool(self):
        out = execute_tool("get_time", {})
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_execute_unknown_tool(self):
        self.assertEqual(execute_tool("ghost_tool", {}), "未知工具: ghost_tool")

    def test_execute_swallows_tool_exception(self):
        def _boom(**kwargs):
            raise ValueError("炸了")

        register_tool("__boom__", "测试用", _boom, {"type": "object", "properties": {}})
        self.addCleanup(lambda: TOOLS.remove(
            next(t for t in TOOLS if t["name"] == "__boom__")))
        out = execute_tool("__boom__", {})
        self.assertTrue(out.startswith("工具执行失败:"))
        self.assertIn("炸了", out)


class ChunkDocumentTest(unittest.TestCase):
    def test_splits_by_heading_with_ids(self):
        content = "# 标题\n\n第一段内容。\n\n## 小节\n\n第二段内容。"
        result = json.loads(kb_tools.chunk_document(content, source="doc.md"))
        self.assertGreaterEqual(result["total_chunks"], 1)
        self.assertEqual(result["source"], "doc.md")
        # 只预览前 5 个分片
        self.assertLessEqual(len(result["chunks"]), 5)
        for chunk in result["chunks"]:
            self.assertTrue(chunk["id"].startswith("doc.md#"))
            self.assertIn("content", chunk)
            self.assertEqual(chunk["metadata"]["source"], "doc.md")

    def test_empty_content_yields_no_chunks(self):
        result = json.loads(kb_tools.chunk_document("", source="empty.md"))
        self.assertEqual(result["total_chunks"], 0)

    def test_paragraphs_packed_up_to_chunk_size(self):
        # 每段 ~50 字，chunk_size=120 → 一个分片应能装下多段
        para = "这是一段大约五十个字的测试文本，用来验证分片是否可以合并多个段落。" * 1
        content = "# H\n\n" + "\n\n".join([para] * 6)
        result = json.loads(kb_tools.chunk_document(content, source="s.md",
                                                   chunk_size=120))
        self.assertGreater(result["total_chunks"], 1)


class _FakeClient:
    def __init__(self, **responses):
        self.responses = responses

    def search(self, kb_name, query, k=4):
        return self.responses["search"]

    def ingest(self, kb_name, entries):
        return self.responses["ingest"]

    def list_kb(self):
        return self.responses["list_kb"]

    def delete_kb(self, kb_name):
        return self.responses["delete_kb"]

    def delete_entries(self, kb_name, ids):
        return self.responses.get("delete_entries", {"ok": True, "deleted": len(ids)})


class KbToolFormattingTest(unittest.TestCase):
    def _patch_client(self, **responses):
        p = mock.patch.object(kb_tools, "_get_client",
                              lambda: _FakeClient(**responses))
        p.start()
        self.addCleanup(p.stop)

    def test_search_success(self):
        self._patch_client(search={"ok": True, "docs": [
            {"id": "1", "content": "正文内容",
             "metadata": {"source": "a.md"}, "distance": 0.2},
        ]})
        out = kb_tools.search_kb("interview", "问题")
        self.assertIn("[知识库 'interview' 搜索结果", out)
        self.assertIn("相关度: 0.800", out)
        self.assertIn("正文内容", out)

    def test_search_empty(self):
        self._patch_client(search={"ok": True, "docs": []})
        self.assertIn("未找到相关内容", kb_tools.search_kb("kb", "q"))

    def test_search_failure(self):
        self._patch_client(search={"ok": False, "error": "连接失败"})
        self.assertIn("[搜索失败] 连接失败", kb_tools.search_kb("kb", "q"))

    def test_ingest_success(self):
        self._patch_client(ingest={"ok": True, "count": 3})
        self.assertIn("成功存入 3 条片段",
                      kb_tools.ingest_kb("kb", [{"id": "1", "content": "x"}]))

    def test_list_kb_with_counts(self):
        self._patch_client(list_kb={"ok": True, "collections": ["a", "b"],
                                    "counts": {"a": 2, "b": 0}})
        out = kb_tools.list_kb()
        self.assertIn("a: 2 条", out)
        self.assertIn("b: 0 条", out)

    def test_list_kb_empty(self):
        self._patch_client(list_kb={"ok": True, "collections": [], "counts": {}})
        self.assertIn("暂无知识库", kb_tools.list_kb())

    def test_delete_kb(self):
        self._patch_client(delete_kb={"ok": True, "deleted": True})
        self.assertIn("已删除", kb_tools.delete_kb("kb"))

    def test_delete_entries_requires_ids(self):
        out = kb_tools.delete_entries("kb", [])
        self.assertIn("不能为空", out)

    def test_delete_entries_batch(self):
        p = mock.patch.object(kb_tools, "_get_client", lambda: _FakeClient(
            search={}, ingest={}, list_kb={}, delete_kb={},
            delete_entries={"ok": True, "deleted": 2}))
        p.start()
        self.addCleanup(p.stop)
        self.assertIn("批量删除 2 条片段",
                      kb_tools.delete_entries("kb", ["1", "2"]))


class ChunkSizeTest(unittest.TestCase):
    def test_default_chunk_size_is_600(self):
        result = json.loads(kb_tools.chunk_document("正文", source="s"))
        self.assertEqual(result["chunk_size"], 600)


if __name__ == "__main__":
    unittest.main()
