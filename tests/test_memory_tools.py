# -*- coding: utf-8 -*-
"""memory_tools 单测：不拉起 RAG daemon / 不下载模型，全程 mock。

覆盖：
- 私聊下 QQ 号从上下文注入（collection = memory_<QQ>），模型无法指定 QQ；
- 群聊 / 网页端（无 QQ 上下文）一律拒绝；
- memory_save（自动打标签）/ memory_search / memory_list / memory_delete 的 CRUD 行为。
"""

import unittest
from unittest.mock import patch

from app.tools.rag import memory_tools as mt


class _FakeClient:
    """内存版 RagClient，记录调用、按 collection 存条目。"""

    def __init__(self):
        self.calls = []
        self.store = {}  # kb_name -> {id: {content, metadata}}

    def ingest(self, kb_name, entries):
        self.calls.append(("ingest", kb_name, entries))
        col = self.store.setdefault(kb_name, {})
        for e in entries:
            col[e["id"]] = {"content": e["content"],
                            "metadata": e.get("metadata", {})}
        return {"ok": True, "count": len(entries)}

    def search(self, kb_name, query, k=4):
        self.calls.append(("search", kb_name, query, k))
        col = self.store.get(kb_name, {})
        docs = [{"id": i, "content": v["content"], "metadata": v["metadata"],
                 "distance": 0.1} for i, v in col.items()]
        return {"ok": True, "docs": docs}

    def list_entries(self, kb_name):
        self.calls.append(("list_entries", kb_name))
        col = self.store.get(kb_name, {})
        entries = [{"id": i, "content": v["content"], "metadata": v["metadata"]}
                   for i, v in col.items()]
        return {"ok": True, "entries": entries}

    def delete_entry(self, kb_name, entry_id):
        self.calls.append(("delete_entry", kb_name, entry_id))
        col = self.store.get(kb_name, {})
        existed = entry_id in col
        col.pop(entry_id, None)
        return {"ok": True, "deleted": existed}


class _FakeRagClient:
    _inst = None

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = _FakeClient()
        return cls._inst


class MemoryToolsTest(unittest.TestCase):

    def setUp(self):
        _FakeRagClient._inst = None
        self._c = patch.object(mt, "RagClient", _FakeRagClient).start()
        self.ctx = patch.object(mt, "current_context").start()
        self.ctx.return_value = ("private", "12345")
        self.fake = _FakeRagClient.get()

    def tearDown(self):
        patch.stopall()

    # ── 私聊：QQ 注入 + collection 命名 ──
    def test_save_uses_injected_qq_and_collection(self):
        out = mt.memory_save("我喜欢喝美式咖啡")
        self.assertIn("已保存", out)
        self.assertEqual(len(self.fake.calls), 1)
        cmd, kb, entries = self.fake.calls[0]
        self.assertEqual(cmd, "ingest")
        self.assertEqual(kb, "memory_12345")
        self.assertIn("美式咖啡", entries[0]["content"])
        # 没给标签时 metadata.tags 为空列表
        self.assertEqual(entries[0]["metadata"]["tags"], [])

    # ── 标签解析（逗号 / 顿号 / 空格）──
    def test_save_parses_tags(self):
        mt.memory_save("周五要交方案", tags="工作,计划、 deadline")
        entries = self.fake.store["memory_12345"]
        self.assertEqual(len(entries), 1)
        tag_list = list(entries.values())[0]["metadata"]["tags"]
        self.assertEqual(set(tag_list), {"工作", "计划", "deadline"})
        # 正文里也带上了标签，便于语义检索命中
        self.assertIn("标签", list(entries.values())[0]["content"])

    # ── 群聊拒绝 ──
    def test_reject_group(self):
        self.ctx.return_value = ("group", "999")
        out = mt.memory_save("secret")
        self.assertIn("仅限 QQ 私聊", out)
        self.assertEqual(self.fake.calls, [])

    # ── 网页端（无上下文）拒绝 ──
    def test_reject_web(self):
        self.ctx.return_value = (None, None)
        out = mt.memory_list()
        self.assertIn("仅限 QQ 私聊", out)
        self.assertEqual(self.fake.calls, [])

    # ── 参数校验 ──
    def test_save_requires_content(self):
        self.assertIn("错误", mt.memory_save(""))
        self.assertEqual(self.fake.calls, [])

    # ── list / search ──
    def test_list_and_search(self):
        mt.memory_save("A 偏好用 Claude 写代码", tags="偏好")
        mt.memory_save("B 计划十月去旅行", tags="计划")
        listing = mt.memory_list()
        self.assertIn("记忆列表", listing)
        self.assertIn("Claude", listing)
        self.assertIn("旅行", listing)
        self.assertEqual(len(self.fake.store["memory_12345"]), 2)

        searched = mt.memory_search("写代码用什么")
        self.assertIn("记忆搜索结果", searched)
        self.assertIn("Claude", searched)

    # ── delete ──
    def test_delete_existing_and_missing(self):
        mt.memory_save("临时记一条")
        eid = list(self.fake.store["memory_12345"].keys())[0]
        out = mt.memory_delete(eid)
        self.assertIn("已删除", out)
        out2 = mt.memory_delete(eid)
        self.assertIn("未找到", out2)

    # ── 删除缺 id 校验 ──
    def test_delete_requires_id(self):
        self.assertIn("错误", mt.memory_delete(""))
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
